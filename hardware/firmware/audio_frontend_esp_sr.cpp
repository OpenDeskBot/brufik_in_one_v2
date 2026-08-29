#include "audio_frontend_esp_sr.h"

#include "deskbot_config.h"
#include "logger.h"
/* 仅取 SAMPLE_RATE（16000）常量。 */
#include "speaker.h"

#include "esp_afe_config.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_heap_caps.h"

#include "freertos/queue.h"
#include "freertos/task.h"

#include <Arduino.h>
#include <atomic>
#include <math.h>
#include <string.h>

namespace {

#ifndef DESKBOT_AFE_REFERENCE_DELAY_MS
#define DESKBOT_AFE_REFERENCE_DELAY_MS 32
#endif

/*
 * The WebRTC AGC inside the AFE has two gain stages: a fixed compression
 * gain that applies immediately, and a speech-adaptive stage that converges
 * only while real speech is present.  The fetch worker consumes the AFE
 * continuously from boot, but a quiet room never exercises the adaptive
 * stage, so its speech-level estimate is still cold when the PC binds the
 * RTC session and the user first talks.  Field data: the PC-side utterance
 * normalizer needed 8x gain right after bind and settled at 2x about 30 s
 * later, i.e. the processed uplink level rose by ~12 dB purely through AGC
 * convergence while every other stage (afe_linear_gain, Opus, USB) is
 * fixed.  Raising the immediate compression gain from the SDK default 9 dB
 * narrows that cold-start deficit; the AGC limiter still clamps the
 * envelope to agc_target_level_dbfs, so loud speech cannot clip harder
 * than before.  Field-tunable via -DDESKBOT_AFE_AGC_COMPRESSION_GAIN_DB=<n>.
 *
 * 实测回调：15 dB 会把 AEC 残留一并抬过设备 VAD 阈值——机器人开口
 * 0.15 秒内 speech_start 即被自己的扬声器触发，残留经 ASR 幻听成
 * 长句造成整句自打断。12 dB 折中：冷启动缺口约 9 dB 仍在 PC 端
 * normalizer 8x（18 dB）补偿范围内，残留回到 VAD 阈值之下。
 */
#ifndef DESKBOT_AFE_AGC_COMPRESSION_GAIN_DB
#define DESKBOT_AFE_AGC_COMPRESSION_GAIN_DB 12
#endif

struct AudioIoFrame {
  int16_t pcm[kAudioFrontendIoFrameSamples];
};

constexpr UBaseType_t kInputQueueDepth = 12;
constexpr UBaseType_t kOutputQueueDepth = 20;
constexpr UBaseType_t kVadQueueDepth = 8;
constexpr size_t kReferenceRingSamples = 4096;  // 256 ms @ 16 kHz
constexpr size_t kMaxAfeChunkSamples = 1024;
constexpr size_t kAfeAccumulatorSamples =
    kMaxAfeChunkSamples + kAudioFrontendIoFrameSamples;
constexpr size_t kReferenceDelaySamples =
    static_cast<size_t>(DESKBOT_AFE_REFERENCE_DELAY_MS) * 16U;
constexpr uint32_t kAecDiagnosticsIntervalMs = 5000;
constexpr size_t kCorrelationDownsample = 4;
constexpr size_t kCorrelationHistorySamples = 2048;  // 512 ms @ 4 kHz
constexpr int kCorrelationMaxLagSamples = 640;       // +/-160 ms @ 4 kHz
constexpr uint32_t kCorrelationReferenceRmsFloor = 64;
constexpr uint32_t kProcessedFreezeFrames = 64;  // about 2 s at 32 ms/fetch
constexpr uint32_t kProcessedRecoveryFrames = 16;
constexpr uint32_t kFetchTimeoutMs = 100;
constexpr uint32_t kFetchFailureFallbackThreshold = 3;
/* ESP-SR's raw VAD edge is intentionally stabilised before it reaches the
 * host. The V2 microphone's quiet processed level is commonly -43..-46 dBFS,
 * so a fixed threshold is unreliable across units. Track the local noise
 * floor, require sustained speech above it, and use a longer release window. */
constexpr float kVadStartMarginDb = 7.0f;
constexpr float kVadHoldMarginDb = 3.0f;
constexpr uint8_t kVadStartConfirmFrames = 3;   // ~96 ms
constexpr uint8_t kVadEndConfirmFrames = 8;     // ~256 ms
constexpr uint32_t kVadPlaybackTailMs = 500;
static_assert(kReferenceDelaySamples < kReferenceRingSamples,
              "AEC reference delay must fit in its ring");
/*
 * The V2 LMD2718T PDM microphone produces a healthy but deliberately low
 * digital level.  Keep the gain inside ESP-SR so AEC/NS/AGC remain the sole
 * audio-conditioning pipeline.  This restores the 5x gain used by the
 * pre-ESP-SR path without applying the legacy filter a second time.
 * ESP-SR documents afe_linear_gain as an output multiplier in [0.1, 10.0].
 */
constexpr float kAfeLinearGain = 5.0f;

/* esp_afe_handle_from_config 返回 const（esp_afe_sr_models.h）。 */
const esp_afe_sr_iface_t* s_afe_handle = nullptr;
esp_afe_sr_data_t* s_afe_data = nullptr;
QueueHandle_t s_input_q = nullptr;
QueueHandle_t s_output_q = nullptr;
QueueHandle_t s_vad_q = nullptr;
TaskHandle_t s_feed_task = nullptr;
TaskHandle_t s_fetch_task = nullptr;

int s_feed_samples = 0;
int s_fetch_samples = 0;
int16_t* s_feed_interleaved = nullptr;
int16_t* s_reference_frame = nullptr;
int16_t* s_mic_accumulator = nullptr;
size_t s_mic_accumulator_len = 0;
AudioIoFrame s_output_partial{};
size_t s_output_partial_len = 0;

std::atomic<bool> s_ready{false};
std::atomic<bool> s_vad_speech{false};
std::atomic<uint32_t> s_vad_sequence{0};
bool s_vad_stable_speech = false;
bool s_vad_noise_floor_valid = false;
float s_vad_noise_floor_db = -60.0f;
uint8_t s_vad_start_frames = 0;
uint8_t s_vad_end_frames = 0;
std::atomic<uint32_t> s_vad_playback_tail_until_ms{0};
std::atomic<uint32_t> s_vad_reset_generation{0};
uint32_t s_vad_applied_reset_generation = 0;
std::atomic<uint32_t> s_feed_chunks{0};
std::atomic<uint32_t> s_fetch_chunks{0};
std::atomic<uint32_t> s_fetch_failures{0};
std::atomic<bool> s_output_healthy{true};
uint32_t s_identical_processed_frames = 0;
uint32_t s_processed_recovery_frames = 0;
int16_t s_previous_processed[kMaxAfeChunkSamples]{};
size_t s_previous_processed_samples = 0;
bool s_previous_processed_valid = false;

portMUX_TYPE s_reference_mux = portMUX_INITIALIZER_UNLOCKED;
int16_t s_reference_ring[kReferenceRingSamples]{};
size_t s_reference_read = 0;
size_t s_reference_write = 0;
size_t s_reference_count = 0;
bool s_reference_playback_active = false;
bool s_reference_primed = false;
uint32_t s_reference_input_rate = 0;
uint8_t s_reference_input_channels = 0;
uint32_t s_reference_resample_phase = 0;
std::atomic<uint32_t> s_reference_overruns{0};
std::atomic<uint32_t> s_reference_underflows{0};
std::atomic<uint32_t> s_reference_copied{0};
std::atomic<uint32_t> s_reference_submitted{0};
std::atomic<uint32_t> s_input_drops{0};
std::atomic<uint32_t> s_output_drops{0};

void reference_push_locked(int16_t sample) {
  if (s_reference_count == kReferenceRingSamples) {
    s_reference_read = (s_reference_read + 1) % kReferenceRingSamples;
    s_reference_count--;
    s_reference_overruns.fetch_add(1, std::memory_order_relaxed);
  }
  s_reference_ring[s_reference_write] = sample;
  s_reference_write = (s_reference_write + 1) % kReferenceRingSamples;
  s_reference_count++;
}

void reference_clear_locked(bool seed_delay = false) {
  s_reference_read = 0;
  s_reference_write = 0;
  s_reference_count = 0;
  s_reference_primed = false;
  s_reference_input_rate = 0;
  s_reference_input_channels = 0;
  s_reference_resample_phase = 0;
  if (seed_delay) {
    /*
     * This is a real delay line, not a startup gate.  Historical playback
     * before the first submitted sample is silence, so seed that silence and
     * let every microphone frame consume a complete, continuous reference.
     */
    for (size_t i = 0; i < kReferenceDelaySamples; ++i) {
      s_reference_ring[s_reference_write] = 0;
      s_reference_write =
          (s_reference_write + 1U) % kReferenceRingSamples;
    }
    s_reference_count = kReferenceDelaySamples;
  }
}

void reference_read_samples(int16_t* dst, size_t samples) {
  if (!dst || samples == 0) {
    return;
  }
  portENTER_CRITICAL(&s_reference_mux);
  size_t copied = 0;
  /*
   * While playback is active, retain a fixed delay line.  Do not attempt to
   * correct scheduler/DMA bursts by deleting samples: the PDM and speaker I2S
   * clocks come from the same ESP32-S3 and a discontinuous reference is much
   * more damaging to an adaptive echo canceller than short-term queue jitter.
   */
  const size_t reserve =
      s_reference_playback_active ? kReferenceDelaySamples : 0U;
  if (!s_reference_primed) {
    if ((!s_reference_playback_active && s_reference_count > 0) ||
        s_reference_count >= reserve + samples) {
      s_reference_primed = true;
    }
  }
  while (s_reference_primed && copied < samples &&
         s_reference_count > reserve) {
    dst[copied++] = s_reference_ring[s_reference_read];
    s_reference_read = (s_reference_read + 1) % kReferenceRingSamples;
    s_reference_count--;
  }
  const bool count_underflow =
      s_reference_playback_active && s_reference_primed && copied < samples;
  portEXIT_CRITICAL(&s_reference_mux);
  s_reference_copied.fetch_add(
      static_cast<uint32_t>(copied), std::memory_order_relaxed);
  if (copied < samples) {
    /*
     * Missing historical playback is silence.  Repeating the last non-zero
     * sample creates a synthetic DC step that is not present at the speaker
     * and prevents the AEC filter from converging after a producer burst.
     */
    memset(dst + copied, 0, (samples - copied) * sizeof(int16_t));
    if (count_underflow) {
      s_reference_underflows.fetch_add(
          static_cast<uint32_t>(samples - copied),
          std::memory_order_relaxed);
    }
  }
}

void queue_output_drop_oldest(const AudioIoFrame& frame) {
  if (!s_output_q) {
    return;
  }
  while (xQueueSend(s_output_q, &frame, 0) != pdTRUE) {
    AudioIoFrame discarded{};
    if (xQueueReceive(s_output_q, &discarded, 0) != pdTRUE) {
      break;
    }
    s_output_drops.fetch_add(1, std::memory_order_relaxed);
  }
}

void emit_processed_pcm(const int16_t* pcm, size_t samples) {
  if (!pcm || samples == 0) {
    return;
  }
  size_t offset = 0;
  while (offset < samples) {
    size_t copy = kAudioFrontendIoFrameSamples - s_output_partial_len;
    if (copy > samples - offset) {
      copy = samples - offset;
    }
    memcpy(
        s_output_partial.pcm + s_output_partial_len,
        pcm + offset,
        copy * sizeof(int16_t));
    s_output_partial_len += copy;
    offset += copy;
    if (s_output_partial_len == kAudioFrontendIoFrameSamples) {
      queue_output_drop_oldest(s_output_partial);
      s_output_partial_len = 0;
    }
  }
}

void emit_vad_transition(vad_state_t state, float volume_db) {
  const bool speech = state == VAD_SPEECH;
  const bool previous =
      s_vad_speech.exchange(speech, std::memory_order_acq_rel);
  if (speech == previous) {
    return;
  }
  AudioFrontendVadEvent event{};
  event.state = speech ? AudioFrontendVadState::kSpeech
                       : AudioFrontendVadState::kSilence;
  event.sequence =
      s_vad_sequence.fetch_add(1, std::memory_order_acq_rel) + 1;
  const long scaled = lroundf(volume_db * 100.0f);
  event.volume_db_x100 =
      static_cast<int16_t>(constrain(scaled, -32768L, 32767L));
  if (s_vad_q && xQueueSend(s_vad_q, &event, 0) != pdTRUE) {
    AudioFrontendVadEvent discarded{};
    (void)xQueueReceive(s_vad_q, &discarded, 0);
    (void)xQueueSend(s_vad_q, &event, 0);
  }
}

void reset_feed_processing_state() {
  s_mic_accumulator_len = 0;
}

bool vad_playback_or_tail_active() {
  bool playback_active = false;
  portENTER_CRITICAL(&s_reference_mux);
  playback_active = s_reference_playback_active;
  portEXIT_CRITICAL(&s_reference_mux);
  return playback_active ||
         static_cast<int32_t>(
             s_vad_playback_tail_until_ms.load(std::memory_order_acquire) -
             millis()) > 0;
}

void reset_vad_stabilizer() {
  /* Fetch owns all non-atomic stabilizer fields. Other tasks request a reset
   * by generation so playback/transport callbacks never race DSP state. */
  s_vad_reset_generation.fetch_add(1, std::memory_order_acq_rel);
}

void observe_stable_vad(vad_state_t sdk_state, float volume_db) {
  const uint32_t reset_generation =
      s_vad_reset_generation.load(std::memory_order_acquire);
  if (reset_generation != s_vad_applied_reset_generation) {
    s_vad_applied_reset_generation = reset_generation;
    s_vad_stable_speech = false;
    s_vad_start_frames = 0;
    s_vad_end_frames = 0;
  }
  if (!isfinite(volume_db)) {
    volume_db = -120.0f;
  }
  volume_db = constrain(volume_db, -120.0f, 0.0f);
  const bool sdk_speech = sdk_state == VAD_SPEECH;
  const bool playback_blocked = vad_playback_or_tail_active();

  /* Learn only non-speech/no-playback ambience. Fast initial acquisition and
   * slow tracking thereafter prevent speech itself from raising the floor. */
  if (!sdk_speech && !playback_blocked) {
    if (!s_vad_noise_floor_valid) {
      s_vad_noise_floor_db = volume_db;
      s_vad_noise_floor_valid = true;
    } else {
      s_vad_noise_floor_db =
          s_vad_noise_floor_db * 0.98f + volume_db * 0.02f;
    }
    s_vad_noise_floor_db = constrain(s_vad_noise_floor_db, -90.0f, -20.0f);
  }

  const float noise_floor =
      s_vad_noise_floor_valid ? s_vad_noise_floor_db : -55.0f;
  const float required_margin =
      s_vad_stable_speech ? kVadHoldMarginDb : kVadStartMarginDb;
  const bool credible_speech =
      sdk_speech && !playback_blocked &&
      volume_db >= noise_floor + required_margin;

  if (!s_vad_stable_speech) {
    s_vad_end_frames = 0;
    if (credible_speech) {
      if (s_vad_start_frames < UINT8_MAX) {
        ++s_vad_start_frames;
      }
      if (s_vad_start_frames >= kVadStartConfirmFrames) {
        s_vad_stable_speech = true;
        s_vad_start_frames = 0;
        emit_vad_transition(VAD_SPEECH, volume_db);
      }
    } else {
      s_vad_start_frames = 0;
    }
    return;
  }

  if (credible_speech) {
    s_vad_end_frames = 0;
    return;
  }
  if (s_vad_end_frames < UINT8_MAX) {
    ++s_vad_end_frames;
  }
  if (s_vad_end_frames >= kVadEndConfirmFrames) {
    s_vad_stable_speech = false;
    s_vad_end_frames = 0;
    emit_vad_transition(VAD_SILENCE, volume_db);
  }
}

struct SignalStats {
  int16_t minimum = 0;
  int16_t maximum = 0;
  uint32_t mean_abs = 0;
  uint32_t rms = 0;
  uint64_t abs_sum = 0;
  uint64_t square_sum = 0;
  uint64_t samples = 0;
};

SignalStats signal_stats(
    const int16_t* pcm,
    size_t samples,
    size_t stride = 1,
    bool need_rms = false) {
  SignalStats stats{};
  if (!pcm || samples == 0 || stride == 0) {
    return stats;
  }
  int16_t minimum = INT16_MAX;
  int16_t maximum = INT16_MIN;
  uint64_t abs_sum = 0;
  uint64_t square_sum = 0;
  for (size_t i = 0; i < samples; ++i) {
    const int16_t sample = pcm[i * stride];
    if (sample < minimum) {
      minimum = sample;
    }
    if (sample > maximum) {
      maximum = sample;
    }
    abs_sum += static_cast<uint32_t>(
        sample == INT16_MIN ? 32768 : abs(sample));
    const int64_t wide = static_cast<int64_t>(sample);
    square_sum += static_cast<uint64_t>(wide * wide);
  }
  stats.minimum = minimum;
  stats.maximum = maximum;
  stats.mean_abs = static_cast<uint32_t>(abs_sum / samples);
  /* rms 仅 fetch 路径（VAD 音量）需要；feed/健康检测免去每帧 sqrt。 */
  if (need_rms) {
    stats.rms = static_cast<uint32_t>(
        sqrt(static_cast<double>(square_sum) / static_cast<double>(samples)));
  }
  stats.abs_sum = abs_sum;
  stats.square_sum = square_sum;
  stats.samples = samples;
  return stats;
}

struct SignalWindow {
  uint64_t abs_sum = 0;
  uint64_t square_sum = 0;
  uint64_t samples = 0;
  uint32_t peak = 0;
};

portMUX_TYPE s_diagnostics_mux = portMUX_INITIALIZER_UNLOCKED;
SignalWindow s_diagnostic_mic{};
SignalWindow s_diagnostic_reference{};
SignalWindow s_diagnostic_processed{};
int16_t* s_correlation_mic = nullptr;
int16_t* s_correlation_reference = nullptr;
size_t s_correlation_write = 0;
size_t s_correlation_count = 0;

uint32_t signal_peak(const SignalStats& stats) {
  const uint32_t negative =
      stats.minimum == INT16_MIN
          ? 32768U
          : static_cast<uint32_t>(abs(static_cast<int>(stats.minimum)));
  const uint32_t positive =
      stats.maximum < 0 ? 0U : static_cast<uint32_t>(stats.maximum);
  return negative > positive ? negative : positive;
}

void diagnostic_accumulate(
    SignalWindow& window,
    const SignalStats& stats) {
  const uint32_t peak = signal_peak(stats);
  portENTER_CRITICAL(&s_diagnostics_mux);
  window.abs_sum += stats.abs_sum;
  window.square_sum += stats.square_sum;
  window.samples += stats.samples;
  if (peak > window.peak) {
    window.peak = peak;
  }
  portEXIT_CRITICAL(&s_diagnostics_mux);
}

void diagnostic_take_windows(
    SignalWindow* mic,
    SignalWindow* reference,
    SignalWindow* processed) {
  if (!mic || !reference || !processed) {
    return;
  }
  portENTER_CRITICAL(&s_diagnostics_mux);
  *mic = s_diagnostic_mic;
  *reference = s_diagnostic_reference;
  *processed = s_diagnostic_processed;
  s_diagnostic_mic = {};
  s_diagnostic_reference = {};
  s_diagnostic_processed = {};
  portEXIT_CRITICAL(&s_diagnostics_mux);
}

uint32_t window_rms(const SignalWindow& window) {
  if (window.samples == 0) {
    return 0;
  }
  return static_cast<uint32_t>(sqrt(
      static_cast<double>(window.square_sum) /
      static_cast<double>(window.samples)));
}

void append_correlation_history(
    const int16_t* mic,
    const int16_t* reference,
    size_t samples) {
  if (!mic || !reference || !s_correlation_mic ||
      !s_correlation_reference || samples < kCorrelationDownsample) {
    return;
  }
  for (size_t offset = 0;
       offset + kCorrelationDownsample <= samples;
       offset += kCorrelationDownsample) {
    int32_t mic_sum = 0;
    int32_t reference_sum = 0;
    for (size_t i = 0; i < kCorrelationDownsample; ++i) {
      mic_sum += mic[offset + i];
      reference_sum += reference[offset + i];
    }
    s_correlation_mic[s_correlation_write] = static_cast<int16_t>(
        mic_sum / static_cast<int32_t>(kCorrelationDownsample));
    s_correlation_reference[s_correlation_write] = static_cast<int16_t>(
        reference_sum / static_cast<int32_t>(kCorrelationDownsample));
    s_correlation_write =
        (s_correlation_write + 1U) % kCorrelationHistorySamples;
    if (s_correlation_count < kCorrelationHistorySamples) {
      ++s_correlation_count;
    }
  }
}

double normalized_correlation_for_lag(
    size_t oldest,
    size_t samples,
    int lag) {
  const size_t displacement =
      static_cast<size_t>(lag < 0 ? -lag : lag);
  if (!s_correlation_mic || !s_correlation_reference ||
      displacement >= samples) {
    return 0.0;
  }
  const size_t mic_start = lag >= 0 ? displacement : 0U;
  const size_t reference_start = lag >= 0 ? 0U : displacement;
  const size_t overlap = samples - displacement;
  size_t mic_index = (oldest + mic_start) % kCorrelationHistorySamples;
  size_t reference_index =
      (oldest + reference_start) % kCorrelationHistorySamples;
  int64_t mic_sum = 0;
  int64_t reference_sum = 0;
  int64_t mic_square_sum = 0;
  int64_t reference_square_sum = 0;
  int64_t cross_sum = 0;
  for (size_t i = 0; i < overlap; ++i) {
    const int64_t mic_sample = s_correlation_mic[mic_index];
    const int64_t reference_sample =
        s_correlation_reference[reference_index];
    mic_sum += mic_sample;
    reference_sum += reference_sample;
    mic_square_sum += mic_sample * mic_sample;
    reference_square_sum += reference_sample * reference_sample;
    cross_sum += mic_sample * reference_sample;
    if (++mic_index == kCorrelationHistorySamples) {
      mic_index = 0;
    }
    if (++reference_index == kCorrelationHistorySamples) {
      reference_index = 0;
    }
  }
  const double n = static_cast<double>(overlap);
  const double covariance =
      n * static_cast<double>(cross_sum) -
      static_cast<double>(mic_sum) * static_cast<double>(reference_sum);
  const double mic_variance =
      n * static_cast<double>(mic_square_sum) -
      static_cast<double>(mic_sum) * static_cast<double>(mic_sum);
  const double reference_variance =
      n * static_cast<double>(reference_square_sum) -
      static_cast<double>(reference_sum) *
          static_cast<double>(reference_sum);
  if (mic_variance <= 0.0 || reference_variance <= 0.0) {
    return 0.0;
  }
  return covariance / sqrt(mic_variance * reference_variance);
}

struct CorrelationResult {
  bool valid = false;
  int lag_16khz_samples = 0;
  int correlation_x1000 = 0;
  uint32_t reference_rms = 0;
};

CorrelationResult calculate_reference_correlation() {
  CorrelationResult result{};
  if (!s_correlation_mic || !s_correlation_reference ||
      s_correlation_count < 512U) {
    return result;
  }
  const size_t samples = s_correlation_count;
  const size_t oldest =
      (s_correlation_write + kCorrelationHistorySamples - samples) %
      kCorrelationHistorySamples;
  uint64_t reference_square_sum = 0;
  for (size_t i = 0; i < samples; ++i) {
    const size_t index = (oldest + i) % kCorrelationHistorySamples;
    const int64_t sample = s_correlation_reference[index];
    reference_square_sum += static_cast<uint64_t>(sample * sample);
  }
  result.reference_rms = static_cast<uint32_t>(sqrt(
      static_cast<double>(reference_square_sum) /
      static_cast<double>(samples)));
  if (result.reference_rms < kCorrelationReferenceRmsFloor) {
    return result;
  }

  const int max_lag =
      static_cast<int>(samples / 2U) < kCorrelationMaxLagSamples
          ? static_cast<int>(samples / 2U)
          : kCorrelationMaxLagSamples;
  int best_lag = 0;
  double best_correlation = 0.0;
  constexpr int kCoarseLagStep = 4;
  for (int lag = -max_lag; lag <= max_lag; lag += kCoarseLagStep) {
    const double correlation = normalized_correlation_for_lag(
        oldest,
        samples,
        lag);
    if (fabs(correlation) > fabs(best_correlation)) {
      best_correlation = correlation;
      best_lag = lag;
    }
  }
  const int refine_start =
      best_lag - kCoarseLagStep < -max_lag
          ? -max_lag
          : best_lag - kCoarseLagStep;
  const int refine_end =
      best_lag + kCoarseLagStep > max_lag
          ? max_lag
          : best_lag + kCoarseLagStep;
  for (int lag = refine_start; lag <= refine_end; ++lag) {
    const double correlation = normalized_correlation_for_lag(
        oldest,
        samples,
        lag);
    if (fabs(correlation) > fabs(best_correlation)) {
      best_correlation = correlation;
      best_lag = lag;
    }
  }
  result.valid = true;
  result.lag_16khz_samples =
      best_lag * static_cast<int>(kCorrelationDownsample);
  result.correlation_x1000 =
      static_cast<int>(lround(best_correlation * 1000.0));
  return result;
}

void feed_accumulated_audio() {
  if (!s_afe_handle || !s_afe_data || s_feed_samples <= 0) {
    return;
  }
  while (s_mic_accumulator_len >= static_cast<size_t>(s_feed_samples)) {
    reference_read_samples(
        s_reference_frame, static_cast<size_t>(s_feed_samples));
    const SignalStats mic_stats = signal_stats(
        s_mic_accumulator, static_cast<size_t>(s_feed_samples));
    const SignalStats reference_stats = signal_stats(
        s_reference_frame, static_cast<size_t>(s_feed_samples));
    diagnostic_accumulate(s_diagnostic_mic, mic_stats);
    diagnostic_accumulate(s_diagnostic_reference, reference_stats);
#if DESKBOT_AEC_CORRELATION_DIAGNOSTICS
    append_correlation_history(
        s_mic_accumulator,
        s_reference_frame,
        static_cast<size_t>(s_feed_samples));
#endif

    /* ESP-SR expects channel-interleaved MR input. */
    for (int i = 0; i < s_feed_samples; ++i) {
      s_feed_interleaved[i * 2] = s_mic_accumulator[i];
      s_feed_interleaved[i * 2 + 1] = s_reference_frame[i];
    }
    const int accepted = s_afe_handle->feed(s_afe_data, s_feed_interleaved);
    if (accepted < 0) {
      log_error("[ESP-SR] AFE feed failed rc=%d", accepted);
    } else {
      s_feed_chunks.fetch_add(1, std::memory_order_relaxed);
    }
    s_mic_accumulator_len -= static_cast<size_t>(s_feed_samples);
    if (s_mic_accumulator_len > 0) {
      memmove(
          s_mic_accumulator,
          s_mic_accumulator + s_feed_samples,
          s_mic_accumulator_len * sizeof(int16_t));
    }
    /* CPU0 Idle is watched by the 5 s task WDT.  ESP-SR's high-performance
     * feed can otherwise immediately consume the next queued chunk forever
     * while full-duplex Opus is active.  One tick per 32 ms AFE chunk gives
     * Idle0 a deterministic service window without starving the AFE queue. */
    vTaskDelay(1);
  }
}

void observe_processed_signal(const int16_t* pcm, size_t samples) {
  if (!pcm || samples == 0 || samples > kMaxAfeChunkSamples) {
    return;
  }
  const SignalStats stats = signal_stats(pcm, samples);
  const bool identical =
      s_previous_processed_valid &&
      s_previous_processed_samples == samples &&
      memcmp(
          s_previous_processed, pcm, samples * sizeof(int16_t)) == 0;
  memcpy(s_previous_processed, pcm, samples * sizeof(int16_t));
  s_previous_processed_samples = samples;
  s_previous_processed_valid = true;

  /*
   * AFE noise suppression legitimately emits bit-identical all-zero frames
   * in a quiet room. Only a repeated non-silent waveform is evidence of a
   * wedged DSP output.
   */
  const bool suspicious_identical = identical && stats.mean_abs > 32U;
  if (suspicious_identical) {
    if (s_identical_processed_frames < UINT32_MAX) {
      ++s_identical_processed_frames;
    }
    s_processed_recovery_frames = 0;
  } else {
    s_identical_processed_frames = 0;
    if (!s_output_healthy.load(std::memory_order_acquire) &&
        s_processed_recovery_frames < UINT32_MAX) {
      ++s_processed_recovery_frames;
    }
  }

  if (s_output_healthy.load(std::memory_order_acquire) &&
      s_identical_processed_frames >= kProcessedFreezeFrames) {
    s_output_healthy.store(false, std::memory_order_release);
    s_processed_recovery_frames = 0;
    log_error(
        "[ESP-SR] processed PCM frozen frames=%u min=%d mean_abs=%u max=%d; "
        "switching uplink to raw microphone PCM",
        static_cast<unsigned>(s_identical_processed_frames),
        static_cast<int>(stats.minimum),
        static_cast<unsigned>(stats.mean_abs),
        static_cast<int>(stats.maximum));
  } else if (!s_output_healthy.load(std::memory_order_acquire) &&
             s_processed_recovery_frames >= kProcessedRecoveryFrames) {
    s_output_healthy.store(true, std::memory_order_release);
    s_processed_recovery_frames = 0;
    log_warn(
        "[ESP-SR] processed PCM recovered; restoring AEC/NS uplink");
  }
}

void audio_frontend_feed_task(void*) {
  AudioIoFrame frame{};
  uint32_t last_stats_ms = millis();
  for (;;) {
    if (xQueueReceive(s_input_q, &frame, pdMS_TO_TICKS(100)) == pdTRUE) {
      if (s_mic_accumulator_len + kAudioFrontendIoFrameSamples <=
          kAfeAccumulatorSamples) {
        memcpy(
            s_mic_accumulator + s_mic_accumulator_len,
            frame.pcm,
            sizeof(frame.pcm));
        s_mic_accumulator_len += kAudioFrontendIoFrameSamples;
        feed_accumulated_audio();
      } else {
        log_warn("[ESP-SR] mic accumulator overflow; resetting feed buffer");
        reset_feed_processing_state();
      }
    }

    const uint32_t now = millis();
    if (static_cast<uint32_t>(now - last_stats_ms) >=
        kAecDiagnosticsIntervalMs) {
      const uint32_t input_drops =
          s_input_drops.exchange(0, std::memory_order_acq_rel);
      const uint32_t output_drops =
          s_output_drops.exchange(0, std::memory_order_acq_rel);
      const uint32_t reference_overruns =
          s_reference_overruns.exchange(0, std::memory_order_acq_rel);
      const uint32_t reference_underflows =
          s_reference_underflows.exchange(0, std::memory_order_acq_rel);
      const uint32_t reference_copied =
          s_reference_copied.exchange(0, std::memory_order_acq_rel);
      const uint32_t reference_submitted =
          s_reference_submitted.exchange(0, std::memory_order_acq_rel);
      const uint32_t feed_chunks =
          s_feed_chunks.exchange(0, std::memory_order_acq_rel);
      const uint32_t fetch_chunks =
          s_fetch_chunks.exchange(0, std::memory_order_acq_rel);
      const uint32_t fetch_failures =
          s_fetch_failures.exchange(0, std::memory_order_acq_rel);
      size_t reference_fill = 0;
      bool playback_active = false;
      portENTER_CRITICAL(&s_reference_mux);
      reference_fill = s_reference_count;
      playback_active = s_reference_playback_active;
      portEXIT_CRITICAL(&s_reference_mux);
      SignalWindow mic_window{};
      SignalWindow reference_window{};
      SignalWindow processed_window{};
      diagnostic_take_windows(
          &mic_window, &reference_window, &processed_window);
#if DESKBOT_AEC_CORRELATION_DIAGNOSTICS
      const CorrelationResult correlation =
          calculate_reference_correlation();
#endif
      const unsigned input_depth =
          s_input_q ? static_cast<unsigned>(uxQueueMessagesWaiting(s_input_q))
                    : 0U;
      const unsigned output_depth =
          s_output_q ? static_cast<unsigned>(uxQueueMessagesWaiting(s_output_q))
                     : 0U;
      log_info(
          "[ESP-SR] flow/5s feed=%u fetch=%u fetch_fail=%u "
          "input_q=%u/%u output_q=%u/%u",
          static_cast<unsigned>(feed_chunks),
          static_cast<unsigned>(fetch_chunks),
          static_cast<unsigned>(fetch_failures),
          input_depth,
          static_cast<unsigned>(kInputQueueDepth),
          output_depth,
          static_cast<unsigned>(kOutputQueueDepth));
      log_info(
          "[ESP-SR] aec/5s active=%d mic_rms=%u mic_peak=%u "
          "ref_rms=%u ref_peak=%u processed_rms=%u processed_peak=%u "
          "ref_fill=%u ref_submit=%u ref_copy=%u ref_zero=%u",
          static_cast<int>(playback_active),
          static_cast<unsigned>(window_rms(mic_window)),
          static_cast<unsigned>(mic_window.peak),
          static_cast<unsigned>(window_rms(reference_window)),
          static_cast<unsigned>(reference_window.peak),
          static_cast<unsigned>(window_rms(processed_window)),
          static_cast<unsigned>(processed_window.peak),
          static_cast<unsigned>(reference_fill),
          static_cast<unsigned>(reference_submitted),
          static_cast<unsigned>(reference_copied),
          static_cast<unsigned>(reference_underflows));
#if DESKBOT_AEC_CORRELATION_DIAGNOSTICS
      if (correlation.valid) {
        const int lag_ms_x10 = static_cast<int>(
            static_cast<int64_t>(correlation.lag_16khz_samples) * 10000LL /
            SAMPLE_RATE);
        log_info(
            "[ESP-SR] aec_align/5s corr_x1000=%d lag_samples=%d "
            "lag_ms_x10=%d ref_rms=%u "
            "(positive=mic echo after reference; negative=reference late)",
            correlation.correlation_x1000,
            correlation.lag_16khz_samples,
            lag_ms_x10,
            static_cast<unsigned>(correlation.reference_rms));
      } else if (playback_active) {
        log_info(
            "[ESP-SR] aec_align/5s skipped ref_rms=%u floor=%u",
            static_cast<unsigned>(correlation.reference_rms),
            static_cast<unsigned>(kCorrelationReferenceRmsFloor));
      }
#endif
      if (input_drops || output_drops || reference_overruns ||
          reference_underflows ||
          fetch_failures || (feed_chunks > 0 && fetch_chunks == 0)) {
        log_warn(
            "[ESP-SR] queue/ref stats mic_drop=%u out_drop=%u "
            "ref_overrun=%u ref_zero_samples=%u",
            static_cast<unsigned>(input_drops),
            static_cast<unsigned>(output_drops),
            static_cast<unsigned>(reference_overruns),
            static_cast<unsigned>(reference_underflows));
      }
      last_stats_ms = now;
    }
  }
}

void audio_frontend_fetch_task(void*) {
  uint32_t consecutive_fetch_failures = 0;
  for (;;) {
    /*
     * The default fetch() can block for two seconds and has previously stopped
     * making progress altogether under full-duplex backpressure.  Use the
     * official bounded variant so a wedged AFE cannot strand the fetch worker
     * or turn a degradable DSP fault into a whole-device supervisor restart.
     */
    afe_fetch_result_t* result = s_afe_handle->fetch_with_delay(
        s_afe_data, pdMS_TO_TICKS(kFetchTimeoutMs));
    if (!result || result->ret_value != ESP_OK) {
      s_fetch_failures.fetch_add(1, std::memory_order_relaxed);
      if (consecutive_fetch_failures < UINT32_MAX) {
        ++consecutive_fetch_failures;
      }
      if (consecutive_fetch_failures >=
          kFetchFailureFallbackThreshold) {
        audio_frontend_force_raw_fallback("fetch_timeout_or_error");
      }
      vTaskDelay(1);
      continue;
    }
    consecutive_fetch_failures = 0;
    s_fetch_chunks.fetch_add(1, std::memory_order_relaxed);

    float vad_volume_db = result->data_volume;
    if (result->data && result->data_size > 0) {
      const size_t processed_samples =
          static_cast<size_t>(result->data_size) / sizeof(int16_t);
      const SignalStats processed_stats = signal_stats(
          result->data, processed_samples, /*stride=*/1, /*need_rms=*/true);
      diagnostic_accumulate(s_diagnostic_processed, processed_stats);
      observe_processed_signal(result->data, processed_samples);
      emit_processed_pcm(
          result->data,
          processed_samples);
      /* Some ESP-SR builds report data_volume as a constant zero.  Preserve
       * SDK values when useful, otherwise publish a real processed PCM dBFS
       * value so host-side VAD diagnostics can distinguish echo from speech. */
      if (!isfinite(vad_volume_db) || vad_volume_db == 0.0f) {
        vad_volume_db = processed_stats.rms > 0
                            ? 20.0f * log10f(
                                  static_cast<float>(processed_stats.rms) /
                                  32768.0f)
                            : -120.0f;
      }
    }
    observe_stable_vad(result->vad_state, vad_volume_db);
  }
}

void cleanup_failed_setup() {
  s_ready.store(false, std::memory_order_release);
  if (s_feed_task) {
    vTaskDelete(s_feed_task);
    s_feed_task = nullptr;
  }
  if (s_fetch_task) {
    vTaskDelete(s_fetch_task);
    s_fetch_task = nullptr;
  }
  if (s_afe_handle && s_afe_data) {
    s_afe_handle->destroy(s_afe_data);
  }
  s_afe_data = nullptr;
  s_afe_handle = nullptr;
  if (s_feed_interleaved) {
    heap_caps_free(s_feed_interleaved);
    s_feed_interleaved = nullptr;
  }
  if (s_reference_frame) {
    heap_caps_free(s_reference_frame);
    s_reference_frame = nullptr;
  }
  if (s_mic_accumulator) {
    heap_caps_free(s_mic_accumulator);
    s_mic_accumulator = nullptr;
  }
  if (s_correlation_mic) {
    heap_caps_free(s_correlation_mic);
    s_correlation_mic = nullptr;
    s_correlation_reference = nullptr;
  }
  s_correlation_write = 0;
  s_correlation_count = 0;
  if (s_input_q) {
    vQueueDelete(s_input_q);
    s_input_q = nullptr;
  }
  if (s_output_q) {
    vQueueDelete(s_output_q);
    s_output_q = nullptr;
  }
  if (s_vad_q) {
    vQueueDelete(s_vad_q);
    s_vad_q = nullptr;
  }
}

}  // namespace

bool audio_frontend_setup() {
  if (s_ready.load(std::memory_order_acquire)) {
    return true;
  }
  if (s_feed_task || s_fetch_task || s_afe_data || s_afe_handle) {
    log_error("[ESP-SR] inconsistent prior setup; raw-mic fallback retained");
    return false;
  }

  s_input_q = xQueueCreate(kInputQueueDepth, sizeof(AudioIoFrame));
  s_output_q = xQueueCreate(kOutputQueueDepth, sizeof(AudioIoFrame));
  s_vad_q = xQueueCreate(kVadQueueDepth, sizeof(AudioFrontendVadEvent));
  if (!s_input_q || !s_output_q || !s_vad_q) {
    log_error("[ESP-SR] queue allocation failed");
    cleanup_failed_setup();
    return false;
  }

  srmodel_list_t no_models{};
#if DESKBOT_AFE_LOW_COST
  constexpr afe_mode_t kSelectedAfeMode = AFE_MODE_LOW_COST;
  constexpr aec_mode_t kSelectedAecMode = AEC_MODE_VOIP_LOW_COST;
#else
  constexpr afe_mode_t kSelectedAfeMode = AFE_MODE_HIGH_PERF;
  constexpr aec_mode_t kSelectedAecMode = AEC_MODE_VOIP_HIGH_PERF;
#endif
  afe_config_t* config =
      afe_config_init("MR", &no_models, AFE_TYPE_VC, kSelectedAfeMode);
  if (!config) {
    log_error("[ESP-SR] afe_config_init failed");
    cleanup_failed_setup();
    return false;
  }

  config->aec_init = true;
  config->aec_mode = kSelectedAecMode;
  config->aec_filter_length = 4;
  config->se_init = false;
  config->ns_init = true;
  config->ns_model_name = nullptr;
  config->afe_ns_mode = AFE_NS_MODE_WEBRTC;
  config->vad_init = true;
  config->vad_mode = VAD_MODE_2;
  config->vad_model_name = nullptr;
  config->vad_min_speech_ms = 160;
  config->vad_min_noise_ms = 500;
  config->vad_delay_ms = 160;
  config->vad_mute_playback = false;
  config->vad_enable_channel_trigger = false;
  config->wakenet_init = false;
  config->wakenet_model_name = nullptr;
  config->wakenet_model_name_2 = nullptr;
  config->agc_init = true;
  config->agc_mode = AFE_AGC_MODE_WEBRTC;
  config->agc_compression_gain_db = DESKBOT_AFE_AGC_COMPRESSION_GAIN_DB;
  config->agc_target_level_dbfs = 3;
  config->afe_mode = kSelectedAfeMode;
  config->afe_type = AFE_TYPE_VC;
  config->afe_perferred_core = 0;
  config->afe_perferred_priority = 5;
  config->afe_ringbuf_size = 24;
  config->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
  config->afe_linear_gain = kAfeLinearGain;
  config->debug_init = false;
  // WakeNet is disabled, so there is no "first wake-up" channel to pin.
  config->fixed_first_channel = false;

  config = afe_config_check(config);
  if (!config || !config->aec_init || !config->ns_init ||
      !config->vad_init || config->pcm_config.total_ch_num != 2 ||
      config->pcm_config.mic_num != 1 || config->pcm_config.ref_num != 1) {
    log_error("[ESP-SR] AFE config rejected MR AEC/NS/VAD pipeline");
    if (config) {
      afe_config_free(config);
    }
    cleanup_failed_setup();
    return false;
  }

  s_afe_handle = esp_afe_handle_from_config(config);
  if (s_afe_handle) {
    s_afe_data = s_afe_handle->create_from_config(config);
  }
  afe_config_free(config);
  if (!s_afe_handle || !s_afe_data || !s_afe_handle->fetch_with_delay) {
    if (s_afe_handle && s_afe_data && !s_afe_handle->fetch_with_delay) {
      log_error(
          "[ESP-SR] bounded fetch unavailable; raw-mic fallback retained");
    } else {
      log_error("[ESP-SR] AFE create failed");
    }
    cleanup_failed_setup();
    return false;
  }

  s_feed_samples = s_afe_handle->get_feed_chunksize(s_afe_data);
  s_fetch_samples = s_afe_handle->get_fetch_chunksize(s_afe_data);
  const int channels = s_afe_handle->get_feed_channel_num(s_afe_data);
  const int sample_rate = s_afe_handle->get_samp_rate(s_afe_data);
  if (s_feed_samples <= 0 || s_feed_samples > static_cast<int>(kMaxAfeChunkSamples) ||
      s_fetch_samples <= 0 || s_fetch_samples > static_cast<int>(kMaxAfeChunkSamples) ||
      channels != 2 || sample_rate != SAMPLE_RATE) {
    log_error(
        "[ESP-SR] incompatible runtime feed=%d fetch=%d ch=%d sr=%d",
        s_feed_samples,
        s_fetch_samples,
        channels,
        sample_rate);
    cleanup_failed_setup();
    return false;
  }

  s_feed_interleaved = static_cast<int16_t*>(heap_caps_calloc(
      static_cast<size_t>(s_feed_samples) * 2,
      sizeof(int16_t),
      MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  s_reference_frame = static_cast<int16_t*>(heap_caps_calloc(
      static_cast<size_t>(s_feed_samples),
      sizeof(int16_t),
      MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  s_mic_accumulator = static_cast<int16_t*>(heap_caps_calloc(
      kAfeAccumulatorSamples,
      sizeof(int16_t),
      MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (!s_feed_interleaved || !s_reference_frame || !s_mic_accumulator) {
    log_error("[ESP-SR] work-buffer allocation failed");
    cleanup_failed_setup();
    return false;
  }
#if DESKBOT_AEC_CORRELATION_DIAGNOSTICS
  /* Correlation is diagnostic-only. Keep its history out of production. */
  s_correlation_mic = static_cast<int16_t*>(heap_caps_calloc(
      kCorrelationHistorySamples * 2U,
      sizeof(int16_t),
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (s_correlation_mic) {
    s_correlation_reference =
        s_correlation_mic + kCorrelationHistorySamples;
    s_correlation_write = 0;
    s_correlation_count = 0;
  } else {
    s_correlation_reference = nullptr;
    log_warn("[ESP-SR] AEC lag history unavailable; continuing without correlation diagnostics");
  }
#endif

  /*
   * create_from_config() has already instantiated the SDK-validated
   * MR/VC pipeline above.  Do not call the optional runtime enable_* controls
   * here: ESP-SR's ESP32-S3 VC implementation integrates nonlinear NS into
   * the pipeline and reports that a second runtime NS toggle is unsupported,
   * even though NS is active.  Treating that response as setup failure
   * destroyed an otherwise valid AEC/NS/VAD instance and forced raw-mic
   * fallback on every boot.
   */

  TaskHandle_t feed_created = nullptr;
  const BaseType_t feed_task_rc = xTaskCreatePinnedToCore(
      audio_frontend_feed_task,
      "esp_sr_feed",
      8192,
      nullptr,
      5,
      &feed_created,
      PRO_CPU_NUM);
  if (feed_task_rc != pdPASS || !feed_created) {
    log_error("[ESP-SR] feed task create failed rc=%d", feed_task_rc);
    cleanup_failed_setup();
    return false;
  }
  s_feed_task = feed_created;

  TaskHandle_t fetch_created = nullptr;
  const BaseType_t fetch_task_rc = xTaskCreatePinnedToCore(
      audio_frontend_fetch_task,
      "esp_sr_fetch",
      8192,
      nullptr,
      6,
      &fetch_created,
      PRO_CPU_NUM);
  if (fetch_task_rc != pdPASS || !fetch_created) {
    log_error("[ESP-SR] fetch task create failed rc=%d", fetch_task_rc);
    cleanup_failed_setup();
    return false;
  }
  s_fetch_task = fetch_created;
  s_output_healthy.store(true, std::memory_order_release);
  s_identical_processed_frames = 0;
  s_processed_recovery_frames = 0;
  s_previous_processed_samples = 0;
  s_previous_processed_valid = false;
  s_ready.store(true, std::memory_order_release);
  log_warn(
      "[ESP-SR] official AFE ready pipeline=MR/VC-VOIP-%s-AEC/NS/AGC/VAD "
      "feed=%d fetch=%d ref_delay=%dms linear_gain_x100=%d agc_gain_db=%d "
      "workers=split",
      DESKBOT_AFE_LOW_COST ? "LOW-COST" : "HIGH-PERF",
      s_feed_samples,
      s_fetch_samples,
      static_cast<int>(DESKBOT_AFE_REFERENCE_DELAY_MS),
      static_cast<int>(lroundf(kAfeLinearGain * 100.0f)),
      static_cast<int>(DESKBOT_AFE_AGC_COMPRESSION_GAIN_DB));
  if (s_afe_handle->print_pipeline) {
    s_afe_handle->print_pipeline(s_afe_data);
  }
  return true;
}

bool audio_frontend_ready() {
  return s_ready.load(std::memory_order_acquire);
}

void audio_frontend_force_raw_fallback(const char* reason) {
  const bool was_healthy =
      s_output_healthy.exchange(false, std::memory_order_acq_rel);
  if (!was_healthy) {
    return;
  }
  log_error(
      "[ESP-SR] processed output unhealthy reason=%s; "
      "switching uplink to raw microphone PCM",
      reason && reason[0] ? reason : "unknown");
}

bool audio_frontend_submit_mic(const int16_t* pcm, size_t samples) {
  if (!audio_frontend_ready() || !s_input_q || !pcm ||
      samples != kAudioFrontendIoFrameSamples) {
    return false;
  }
  AudioIoFrame frame{};
  memcpy(frame.pcm, pcm, sizeof(frame.pcm));
  if (xQueueSend(s_input_q, &frame, 0) == pdTRUE) {
    return true;
  }
  AudioIoFrame discarded{};
  (void)xQueueReceive(s_input_q, &discarded, 0);
  s_input_drops.fetch_add(1, std::memory_order_relaxed);
  return xQueueSend(s_input_q, &frame, 0) == pdTRUE;
}

bool audio_frontend_take_output_frame(
    int16_t* pcm,
    TickType_t ticks_to_wait) {
  if (!audio_frontend_ready() || !s_output_q || !pcm) {
    return false;
  }
  AudioIoFrame frame{};
  if (xQueueReceive(s_output_q, &frame, ticks_to_wait) != pdTRUE) {
    return false;
  }
  memcpy(pcm, frame.pcm, sizeof(frame.pcm));
  return true;
}

void audio_frontend_set_playback_active(bool active) {
  if (!audio_frontend_ready()) {
    return;
  }
  portENTER_CRITICAL(&s_reference_mux);
  if (active && !s_reference_playback_active) {
    reference_clear_locked(/*seed_delay=*/true);
  }
  s_reference_playback_active = active;
  if (!active) {
    s_reference_input_rate = 0;
    s_reference_input_channels = 0;
    s_reference_resample_phase = 0;
  }
  portEXIT_CRITICAL(&s_reference_mux);
  if (active) {
    reset_vad_stabilizer();
    emit_vad_transition(VAD_SILENCE, -120.0f);
  } else {
    s_vad_playback_tail_until_ms.store(
        millis() + kVadPlaybackTailMs,
        std::memory_order_release);
  }
}

void audio_frontend_abort_playback_reference() {
  if (!audio_frontend_ready()) {
    return;
  }
  portENTER_CRITICAL(&s_reference_mux);
  s_reference_playback_active = false;
  reference_clear_locked();
  portEXIT_CRITICAL(&s_reference_mux);
}

void audio_frontend_submit_playback_reference(
    const int16_t* pcm,
    size_t samples,
    uint32_t sample_rate,
    uint8_t channels) {
  if (!audio_frontend_ready() || !pcm || samples == 0 ||
      sample_rate == 0 || channels == 0 || channels > 2) {
    return;
  }
  const size_t frames = samples / channels;
  if (frames == 0) {
    return;
  }

  portENTER_CRITICAL(&s_reference_mux);
  if (!s_reference_playback_active) {
    portEXIT_CRITICAL(&s_reference_mux);
    return;
  }
  if (s_reference_input_rate != sample_rate ||
      s_reference_input_channels != channels) {
    s_reference_input_rate = sample_rate;
    s_reference_input_channels = channels;
    s_reference_resample_phase = 0;
  }
  size_t pushed = 0;
  for (size_t frame = 0; frame < frames; ++frame) {
    int32_t mono = pcm[frame * channels];
    if (channels == 2) {
      mono = (mono + pcm[frame * channels + 1]) / 2;
    }
    s_reference_resample_phase += SAMPLE_RATE;
    while (s_reference_resample_phase >= sample_rate) {
      reference_push_locked(static_cast<int16_t>(mono));
      s_reference_resample_phase -= sample_rate;
      ++pushed;
    }
  }
  portEXIT_CRITICAL(&s_reference_mux);
  s_reference_submitted.fetch_add(
      static_cast<uint32_t>(pushed), std::memory_order_relaxed);
}

bool audio_frontend_take_vad_event(AudioFrontendVadEvent* event) {
  return event && s_vad_q &&
         xQueueReceive(s_vad_q, event, 0) == pdTRUE;
}
