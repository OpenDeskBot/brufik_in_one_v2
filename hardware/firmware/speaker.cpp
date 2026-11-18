#include "speaker.h"

#include "audio_frontend_esp_sr.h"
#include "pb_model.h"
#include "utils/opus_codec.h"
#include "utils/utils.h"
#include "logger.h"
#include "ws_transport.h"

#include "driver/i2s_std.h"
#include "esp_attr.h"
#include "esp_timer.h"
#include <atomic>
#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "freertos/queue.h"
#include "freertos/task.h"

namespace {

constexpr int kDmaBufCount = 3;
constexpr int kDmaBufLen = audio_lipsync::kBlockFrames;
/** opus_decode 栈开销与 mic 端 encode 同量级；8KB 会触发 stack canary。 */
constexpr uint32_t kSpeakerTaskStack = 28 * 1024;

static std::atomic<bool> s_is_speaking{false};
static std::atomic<float> s_volume{DESKBOT_AUDIO_PLAY_VOLUME};
/** 流式会话：仅 task_loop_speaker 写；pb 经 speaker_stream_pcm_active() 跨任务读 → 保持 atomic。 */
// The IRAM DMA callback reads this while Flash/PSRAM caches can be disabled.
// GCC emits an out-of-line Flash helper for atomic<bool>::load under -Os;
// aligned 32-bit atomic loads compile to cache-independent instructions.
static std::atomic<uint32_t> s_stream_active{0};
/** 跨任务请求取消当前 i2s 写出（abort 置位，见到 cancel 后清位）。 */
static std::atomic<bool> s_need_cancel{false};
static std::atomic<bool> s_cancel_in_progress{false};
/** pb_runtime 查询：当前任务是否已执行完毕（入队时 false，见到 kEndOfTask 后 true）。 */
static std::atomic<bool> s_task_done{true};
static std::atomic<uint32_t> s_buffered_ms{0};
static std::atomic<uint32_t> s_timeline_epoch{0};
static std::atomic<uint32_t> s_timeline_ms{0};
static std::atomic<uint32_t> s_output_empty_periods{0};
static const pb_audio* s_current_audio = nullptr;
// Metadata copies survive ownership release, including an initial packet shorter
// than one DMA period. No PCM pointers are retained for asynchronous reporting.
static pb_audio s_first_audio_meta, s_last_audio_meta;
static uint32_t s_started_epoch = 0;
static uint32_t s_cancel_count = 0;

static uint32_t s_report_epoch=0, s_report_played=0;
static void report_audio(const char* kind, const pb_audio* audio, bool force=false) {
  const uint32_t played=s_timeline_ms.load();
  if (!force && ws_transport_compact_supported() && strcmp(kind,"pb_progress")==0 &&
      audio->epoch==s_report_epoch && played>=s_report_played &&
      played-s_report_played<200 && !speaker_task_done()) return;
  s_report_epoch=audio->epoch; s_report_played=played;
  JsonDocument doc;
  doc["type"] = "pb_ack"; doc["ack_type"] = kind;
  doc["req"] = audio->req; doc["idx"] = audio->idx; doc["epoch"] = audio->epoch;
  doc["device_ms"] = millis(); doc["played_ms"] = s_timeline_ms.load();
  doc["queue_wait_ms"] = millis() - audio->received_ms;
  doc["lipsync_mode"] = "rms";
  doc["output_empty_periods"] = s_output_empty_periods.load();
  (void)ws_transport_enqueue_ack(doc);
  log_warn("[PB_AUDIO] %s req=%s epoch=%u idx=%d played_ms=%u queue_wait_ms=%u", kind,
           audio->req, (unsigned)audio->epoch, audio->idx, (unsigned)s_timeline_ms.load(),
           (unsigned)(millis()-audio->received_ms));
}
static uint32_t s_i2s_rate = SAMPLE_RATE;
/** 当前逻辑声道数；物理线格式固定 stereo-both（NS4168），mono 源由 play() 插值。 */
static uint8_t s_i2s_channels = 1;
static QueueHandle_t s_queue = nullptr;
static TaskHandle_t s_task = nullptr;

// Fixed, owned PCM blocks. The ISR only moves prepared blocks and metadata;
// RMS and all floating point work run in the speaker task before playback.
using OutputBlock = audio_lipsync::PcmBlock;
static std::atomic<uint32_t> s_content_gap_periods{0},s_content_gap_events{0};
static uint32_t s_pending_gap=0,s_seen_content=0;
struct DmaSlot { void* buffer = nullptr; audio_lipsync::Frame lip; };
static DmaSlot s_dma_slots[kDmaBufCount];
static i2s_chan_handle_t s_tx = nullptr;
static QueueHandle_t s_output_queue = nullptr, s_mouth_queue = nullptr;
static std::atomic<uint32_t> s_output_generation{1}, s_output_pending{0};
static uint64_t s_source_samples = 0;
static uint32_t s_content_epoch = 0, s_content_origin_ms = 0;
static OutputBlock s_partial;


static bool IRAM_ATTR on_output_sent(i2s_chan_handle_t, i2s_event_data_t* event, void*) {
  BaseType_t wake = pdFALSE;
  DmaSlot* slot = nullptr;
  for (auto& candidate : s_dma_slots) {
    if (!candidate.buffer || candidate.buffer == event->dma_buf) {
      candidate.buffer = event->dma_buf;
      slot = &candidate;
      break;
    }
  }
  if (!slot) return false;
  auto completed = slot->lip;
  completed.output_us = esp_timer_get_time();
  if (completed.active) s_output_pending.fetch_sub(1, std::memory_order_relaxed);
  if (completed.generation == s_output_generation.load(std::memory_order_relaxed)) {
    if(completed.active) {
      if(s_seen_content && s_pending_gap){s_content_gap_periods.fetch_add(s_pending_gap);s_content_gap_events.fetch_add(1);}
      s_seen_content=1;s_pending_gap=0;
    }
    if (completed.epoch) {
      s_timeline_ms.store(completed.end_ms, std::memory_order_relaxed);
      s_timeline_epoch.store(completed.epoch, std::memory_order_release);
    }
    xQueueOverwriteFromISR(s_mouth_queue, &completed, &wake);
  } else {
    if(s_seen_content)++s_pending_gap;
    // Underrun: output is zero, never replay the last nonzero mouth pose.
    audio_lipsync::Frame silence;
    silence.output_us = completed.output_us;
    silence.generation = s_output_generation.load(std::memory_order_relaxed);
    silence.active = s_stream_active.load(std::memory_order_relaxed);
    silence.underrun = true;
    xQueueOverwriteFromISR(s_mouth_queue, &silence, &wake);
  }
  // auto_clear_before_cb has already zeroed this DMA buffer. Copy by value,
  // so the producer can immediately release/reuse decoded PCM.
  OutputBlock next;
  if (xQueueReceiveFromISR(s_output_queue, &next, &wake) == pdTRUE) {
    memcpy(event->dma_buf, next.pcm, event->size);
    slot->lip = next.lip;
  } else {
    slot->lip = audio_lipsync::Frame{};
    if (s_stream_active.load(std::memory_order_relaxed))
      s_output_empty_periods.fetch_add(1, std::memory_order_relaxed);
  }
  return wake == pdTRUE;
}

// Called only by the playback owner, with TX disabled (no concurrent ISR).
static void reset_output() {
  s_output_generation.fetch_add(1, std::memory_order_release);
  xQueueReset(s_output_queue);
  xQueueReset(s_mouth_queue);
  for (auto& slot : s_dma_slots) {
    if (slot.buffer) memset(slot.buffer, 0, 2 * kDmaBufLen * sizeof(int16_t));
    slot.lip = audio_lipsync::Frame{};
  }
  s_output_pending.store(0);
  s_source_samples = 0;
  s_pending_gap=s_seen_content=0;
  s_content_epoch = s_content_origin_ms = 0;
  s_partial = OutputBlock{};
  s_timeline_epoch.store(0);
}

static void speaker_set_clk(uint32_t rate, uint8_t channels) {
  const uint32_t r = rate ? rate : SAMPLE_RATE;
  i2s_channel_disable(s_tx);
  reset_output();
  s_first_audio_meta = pb_audio{};
  s_last_audio_meta = pb_audio{};
  i2s_std_clk_config_t clock = I2S_STD_CLK_DEFAULT_CONFIG(r);
  if (i2s_channel_reconfig_std_clock(s_tx, &clock) != ESP_OK)
    log_error("[SPEAKER] clock configuration failed rate=%u", (unsigned)r);
  s_i2s_channels = channels == 2 ? 2 : 1;
  s_i2s_rate = r;
  i2s_channel_enable(s_tx);
}

enum class HeapFree : uint8_t { kMalloc = 0, kHeapCaps = 1 };

/** 执行器任务元素 type；cancel 走 xQueue 队尾，作旧/新任务分界。 */
enum class JobType : uint8_t {
  kCancel = 0,
  kWav = 1,
  kBegin = 2,
  kChunk = 3,
  kEnd = 4,
  kPbAudio = 5,
  kEndOfTask = 6,
};

struct Job {
  JobType type = JobType::kWav;
  HeapFree free_mode = HeapFree::kMalloc;
  uint8_t channels = 1;
  uint32_t rate = SAMPLE_RATE;
  pb_audio* pb_audio_ptr = nullptr;
  union {
    struct {
      uint8_t* ptr;
      size_t len;
    } wav;
    struct {
      int16_t* ptr;
      size_t samples;
    } pcm;
  };
};

static void free_ptr(void* p, HeapFree mode) {
  if (!p) {
    return;
  }
  if (mode == HeapFree::kMalloc) {
    ::free(p);
  } else {
    heap_caps_free(p);
  }
}

static void free_job(Job& j) {
  if (j.type == JobType::kWav) {
    free_ptr(j.wav.ptr, j.free_mode);
    j.wav.ptr = nullptr;
  } else if (j.type == JobType::kChunk) {
    free_ptr(j.pcm.ptr, j.free_mode);
    j.pcm.ptr = nullptr;
  } else if (j.type == JobType::kPbAudio) {
    if (j.pb_audio_ptr && j.pb_audio_ptr->epoch) s_buffered_ms.fetch_sub(j.pb_audio_ptr->duration_ms);
    pb_audio_free(j.pb_audio_ptr);
    j.pb_audio_ptr = nullptr;
  }
}

static HeapFree caps_to_mode(uint32_t caps) {
  return (caps == 0) ? HeapFree::kMalloc : HeapFree::kHeapCaps;
}

static bool enqueue(Job& j) {
  /* 满则失败（不从队列偷包，避免与 task_loop_speaker 双消费者竞态）。 */
  if (j.type != JobType::kCancel && j.type != JobType::kEndOfTask) {
    s_task_done.store(false, std::memory_order_release);
  }
  if (j.type == JobType::kChunk || j.type == JobType::kPbAudio) {
    return xQueueSend(s_queue, &j, 0) == pdTRUE;
  }
  return xQueueSend(s_queue, &j, portMAX_DELAY) == pdTRUE;
}

/** 停流；force 时即使未 begin 也清 I2S（WAV 收尾/abort）。 */
static void stop_output(bool graceful, uint8_t channels, bool force);

static void finish_cancel() {
  ++s_cancel_count;
  // Preserve the final DMA progress before cancellation clears the timeline.
  if (ws_transport_compact_supported() && s_last_audio_meta.epoch &&
      s_timeline_epoch.load()==s_last_audio_meta.epoch)
    report_audio("pb_progress",&s_last_audio_meta,true);
  stop_output(/*graceful=*/false, 1, /*force=*/true);
  s_started_epoch = 0; s_timeline_epoch.store(0);
  s_cancel_in_progress.store(false, std::memory_order_release);
  log_info("[SPEAKER] cancel");
}

/**
 * need_cancel==false → false。
 * 否则非阻塞丢弃旧任务，见到 cancel 则清 flag、收尾并 return true（其后新任务保留）。
 * 队列空且未见 cancel → return true，保持 need_cancel。
 */
static bool poll_cancel() {
  if (!s_need_cancel.load(std::memory_order_acquire)) {
    return false;
  }
  Job j{};
  while (xQueueReceive(s_queue, &j, 0) == pdTRUE) {
    if (j.type == JobType::kCancel) {
      s_need_cancel.store(false, std::memory_order_release);
      finish_cancel();
      return true;
    }
    free_job(j);
  }
  return true;
}

// Assemble continuously across PB/stream chunks. Never pad a network boundary.
static bool flush_partial() {
  if (!s_partial.frames) return true;
  const auto timing = s_partial.lip;
  s_partial.lip = audio_lipsync::process_audio(s_partial.pcm, s_partial.frames, 2,
      DESKBOT_LIPSYNC_SILENCE_DB, DESKBOT_LIPSYNC_FULL_DB);
  s_partial.lip.generation = timing.generation;
  s_partial.lip.epoch = timing.epoch;
  s_partial.lip.end_ms = timing.end_ms;
  s_partial.lip.first_sample = timing.first_sample;
  s_output_pending.fetch_add(1);
  while (xQueueSend(s_output_queue, &s_partial, pdMS_TO_TICKS(5)) != pdTRUE) {
    if (s_need_cancel.load()) {
      s_output_pending.fetch_sub(1);
      (void)poll_cancel();
      return false;
    }
  }
  s_partial = OutputBlock{};
  return true;
}

/**
 * 写 I2S：按 s_volume 缩放（可就地改 PCM）。
 * 分块写出以便响应 need_cancel。返回 false 表示中途被取消。
 */
static bool play(int16_t* data, size_t length) {
  if (!data || length == 0) {
    return true;
  }
  if (poll_cancel()) {
    return false;
  }
  if (MAX98357_SD >= 0) digitalWrite(MAX98357_SD, HIGH);
  const float vol = s_volume.load(std::memory_order_relaxed);

  /* 全双工（AEC）：播放不再挡麦，仅标记"正在播音"。 */
  s_is_speaking.store(true, std::memory_order_release);

  if (vol != 1.0f) {
    /* Q15 定点乘，避免每样本 float。speaker_set_volume 已夹到 [0,1]。 */
    const int32_t g = static_cast<int32_t>(vol * 32768.0f + 0.5f);
    for (size_t i = 0; i < length; ++i) {
      data[i] = static_cast<int16_t>((static_cast<int32_t>(data[i]) * g) >> 15);
    }
  }

  // Exactly one physical DMA period per job. Stereo input is downmixed before
  // analysis and duplicated to both amplifier slots; source frames count once.
  const size_t channels = s_i2s_channels;
  if (length % channels) return false;
  for (size_t off = 0; off < length;) {
    if (poll_cancel()) return false;
    if (!s_partial.frames) {
      s_partial.lip.generation = s_output_generation.load();
      s_partial.lip.first_sample = s_source_samples;
    }
    const size_t start = s_partial.frames;
    const size_t frames = s_partial.append(data+off, (length-off)/channels, channels);
    int16_t reference[kDmaBufLen];
    for (size_t i=0; i<frames; ++i) reference[i]=s_partial.pcm[2*(start+i)];
    s_source_samples += frames;
    off += frames * channels;
    if (s_current_audio) {
      s_partial.lip.epoch = s_current_audio->epoch;
      s_partial.lip.end_ms = s_content_origin_ms +
          (s_source_samples * 1000ULL / s_i2s_rate);
    }
    if (audio_frontend_ready())
      audio_frontend_submit_playback_reference(reference, frames, s_i2s_rate, 1);
    if (s_partial.frames == kDmaBufLen && !flush_partial()) return false;
    if (s_current_audio && s_current_audio->epoch &&
        s_timeline_epoch.load() == s_current_audio->epoch &&
        s_started_epoch != s_current_audio->epoch) {
      s_started_epoch = s_current_audio->epoch;
      report_audio("pb_started", &s_first_audio_meta);
    }
  }
  return true;
}

/**
 * Drain every owned DMA block, then reset output to 16k mono and clear old samples.
 * channels 仅保留接口兼容（delay 按 frame 计，与声道无关）。
 */
static void i2s_idle(bool drain, uint8_t /*channels*/) {
  if (drain) {
    (void)flush_partial();
    while (s_output_pending.load(std::memory_order_acquire)) {
      if (s_need_cancel.load()) break;
      vTaskDelay(pdMS_TO_TICKS(2));
    }
    if (!s_need_cancel.load() && s_last_audio_meta.epoch &&
        s_timeline_epoch.load() == s_last_audio_meta.epoch) {
      if (s_started_epoch != s_last_audio_meta.epoch) {
        s_started_epoch = s_last_audio_meta.epoch;
        report_audio("pb_started", &s_first_audio_meta);
      }
      report_audio("pb_progress", &s_last_audio_meta, true);
    }
  }
  speaker_set_clk(SAMPLE_RATE, 1);
}

static void stop_output(bool graceful, uint8_t channels, bool force) {
  if (!s_stream_active.load(std::memory_order_relaxed) && !force) {
    return;
  }
  if (!graceful && MAX98357_SD >= 0) digitalWrite(MAX98357_SD, LOW);
  i2s_idle(/*drain=*/graceful, channels);
  if (MAX98357_SD >= 0) digitalWrite(MAX98357_SD, LOW);
  s_stream_active.store(false, std::memory_order_release);
  s_is_speaking.store(false, std::memory_order_release);
  /* AEC 参考时间线收尾：让声学尾音自然衰减。 */
  audio_frontend_set_playback_active(false);
}

static void end_stream(bool graceful, uint8_t channels) {
  stop_output(graceful, channels, /*force=*/false);
}

static bool play_wav(uint8_t* data, size_t len) {
  if (!data || len < 44 || memcmp(data, "RIFF", 4) != 0 || memcmp(data + 8, "WAVE", 4) != 0) {
    log_error("[SPEAKER] bad WAV header len=%u", (unsigned)len);
    return false;
  }
  const uint16_t channels =
      static_cast<uint16_t>(data[22]) | (static_cast<uint16_t>(data[23]) << 8);
  const uint32_t rate = static_cast<uint32_t>(data[24]) | (static_cast<uint32_t>(data[25]) << 8) |
                        (static_cast<uint32_t>(data[26]) << 16) |
                        (static_cast<uint32_t>(data[27]) << 24);
  const uint16_t bits =
      static_cast<uint16_t>(data[34]) | (static_cast<uint16_t>(data[35]) << 8);
  if (bits != 16) {
    log_error("[SPEAKER] unsupported bits=%u", (unsigned)bits);
    return false;
  }
  if (channels != 1 && channels != 2) {
    log_error("[SPEAKER] unsupported channels=%u", (unsigned)channels);
    return false;
  }

  size_t off = 12;
  uint32_t data_size = 0;
  size_t data_off = 0;
  while (off + 8 <= len) {
    const uint32_t csize =
        static_cast<uint32_t>(data[off + 4]) | (static_cast<uint32_t>(data[off + 5]) << 8) |
        (static_cast<uint32_t>(data[off + 6]) << 16) | (static_cast<uint32_t>(data[off + 7]) << 24);
    if (memcmp(data + off, "data", 4) == 0) {
      data_off = off + 8;
      data_size = csize;
      break;
    }
    off += 8 + csize;
  }
  if (data_off == 0 || data_size == 0 || data_off + data_size > len) {
    log_error("[SPEAKER] WAV data chunk invalid");
    return false;
  }

  speaker_set_clk(rate, static_cast<uint8_t>(channels));
  audio_frontend_set_playback_active(true);
  const bool ok = play(reinterpret_cast<int16_t*>(data + data_off), data_size / 2);
  stop_output(/*graceful=*/ok, static_cast<uint8_t>(channels), /*force=*/true);
  return ok;
}

static bool play_pb_audio_owned(pb_audio* audio) {
  if (!audio || !audio->bin || audio->next_bin_len <= 0) {
    return false;
  }
  if (audio->sr == 0 || audio->ch == 0 || audio->fmt[0] == '\0') {
    log_warn("[SPEAKER] pb_audio missing sr/ch/fmt");
    return false;
  }
  if (strcmp(audio->fmt, "s16le") != 0 && strcmp(audio->fmt, "opus") != 0) {
    log_warn("[SPEAKER] unsupported fmt=%s", audio->fmt);
    return false;
  }

  const uint8_t* payload = reinterpret_cast<const uint8_t*>(audio->bin);
  const size_t length = (size_t)audio->next_bin_len;
  int16_t* pcm_owned = nullptr;
  size_t samples = 0;
  HeapFree free_mode = HeapFree::kHeapCaps;

  if (strcmp(audio->fmt, "opus") == 0) {
    if (audio->ch != 1) return false;  // Existing Opus decoder is mono.
    const uint16_t opus_frames = audio->frames > 0 ? (uint16_t)audio->frames : (uint16_t)1;
    const size_t cap = opus_codec_decode_out_cap((int)audio->sr, opus_frames);
    pcm_owned = (int16_t*)psram_malloc(cap * sizeof(int16_t));
    if (!pcm_owned) {
      return false;
    }
    free_mode = HeapFree::kMalloc;
    const bool batch = audio->frames > 1 || (audio->frames == 1 && length >= 2 &&
        static_cast<size_t>((payload[0] << 8) | payload[1]) == length-2);
    samples = batch ? opus_codec_decode_batch(payload, length, (int)audio->sr, opus_frames,
                                                        pcm_owned, cap)
                              : opus_codec_decode(payload, length, (int)audio->sr, pcm_owned, cap);
    if (samples == 0) {
      free_ptr(pcm_owned, free_mode);
      return false;
    }
    // Encoder pads its last 20 ms frame. Do not turn codec padding into
    // content or let it accumulate between independently synthesized phrases.
    if (audio->duration_ms) {
      const size_t content = static_cast<uint64_t>(audio->sr) * audio->duration_ms / 1000;
      if (samples > content) samples = content;
    }
  } else {
    if ((length & 1u) != 0u) {
      return false;
    }
    pcm_owned = (int16_t*)psram_malloc(length);
    if (!pcm_owned) {
      return false;
    }
    free_mode = HeapFree::kMalloc;
    memcpy(pcm_owned, payload, length);
    samples = length / 2;
  }

  if (!s_stream_active.load(std::memory_order_relaxed)) {
    if (audio->ch != 1 && audio->ch != 2) {
      free_ptr(pcm_owned, free_mode);
      return false;
    }
    speaker_set_clk(audio->sr, audio->ch);
    s_stream_active.store(true, std::memory_order_release);
    audio_frontend_set_playback_active(true);
  }

  if (s_i2s_rate != audio->sr || s_i2s_channels != audio->ch) {
    free_ptr(pcm_owned, free_mode);
    return false;
  }
  if (audio->epoch) {
    const uint64_t expected = s_content_origin_ms + s_source_samples * 1000ULL / s_i2s_rate;
    if (s_content_epoch && (s_content_epoch != audio->epoch ||
        expected > static_cast<uint64_t>(audio->pts_ms)+1 ||
        static_cast<uint64_t>(audio->pts_ms) > expected+1)) {
      log_warn("[SPEAKER] PCM discontinuity epoch=%u pts=%u expected=%llu",
               (unsigned)audio->epoch, (unsigned)audio->pts_ms, (unsigned long long)expected);
      speaker_set_clk(audio->sr, audio->ch);
      audio_frontend_abort_playback_reference();
    }
    if (!s_content_epoch) {
      s_content_epoch = audio->epoch;
      s_content_origin_ms = audio->pts_ms;
    }
  }
  if (s_first_audio_meta.epoch != audio->epoch) {
    s_first_audio_meta = *audio;
    s_first_audio_meta.bin = nullptr;
  }
  s_last_audio_meta = *audio;
  s_last_audio_meta.bin = nullptr;
  s_current_audio = audio;
  const bool ok = play(pcm_owned, samples);
  if (ok && audio->epoch) {
    if (s_started_epoch != audio->epoch && s_output_pending.load()) {
      while (s_timeline_epoch.load() != audio->epoch && !s_need_cancel.load())
        vTaskDelay(pdMS_TO_TICKS(2));
      if (!s_need_cancel.load()) {
        s_started_epoch = audio->epoch;
        report_audio("pb_started", &s_first_audio_meta);
      }
    }
    report_audio("pb_progress", audio);
  }
  s_current_audio = nullptr;
  free_ptr(pcm_owned, free_mode);
  return ok;
}

static void execute_job(Job& job) {
  switch (job.type) {
    case JobType::kWav:
      if (s_stream_active.load(std::memory_order_relaxed)) {
        log_warn("[SPEAKER] wav dropped (stream active)");
      } else if (job.wav.ptr) {
        (void)play_wav(job.wav.ptr, job.wav.len);
      }
      free_job(job);
      break;
    case JobType::kBegin:
      if (s_stream_active.load(std::memory_order_relaxed)) {
        log_warn("[SPEAKER] begin while active -> force end");
        end_stream(/*graceful=*/false, 1);
      }
      if (job.channels != 1 && job.channels != 2) {
        log_warn("[SPEAKER] bad channels=%u", (unsigned)job.channels);
      } else if (job.rate == 0) {
        log_warn("[SPEAKER] bad rate=0");
      } else {
        speaker_set_clk(job.rate, job.channels);
        s_stream_active.store(true, std::memory_order_release);
        audio_frontend_set_playback_active(true);
      }
      break;
    case JobType::kChunk:
      if (!s_stream_active.load(std::memory_order_relaxed)) {
        log_warn("[SPEAKER] chunk dropped (no begin)");
      } else if (job.pcm.ptr && job.pcm.samples > 0) {
        (void)play(job.pcm.ptr, job.pcm.samples);
      }
      free_job(job);
      break;
    case JobType::kPbAudio: {
      const uint32_t cancel_count = s_cancel_count;
      if (job.pb_audio_ptr) {
        if (!play_pb_audio_owned(job.pb_audio_ptr) && job.pb_audio_ptr->epoch && cancel_count == s_cancel_count)
          report_audio("pb_error", job.pb_audio_ptr);
      }
      free_job(job);
      break;
    }
    case JobType::kEnd:
      end_stream(/*graceful=*/true, job.channels);
      break;
    case JobType::kCancel:
      break;
  }
}

static void task_loop_speaker(void*) {
  Job job{};
  constexpr TickType_t kIdleTimeout = pdMS_TO_TICKS(2000);
  for (;;) {
    (void)poll_cancel();
    if (xQueueReceive(s_queue, &job, kIdleTimeout) != pdTRUE) {
      continue;
    }
    if (job.type == JobType::kCancel) {
      /*
       * 空闲时 task 阻塞在 Receive，Cancel 会直接出队，不会经过 poll_cancel。
       * 若不在此清 flag，s_need_cancel 会永久为 true，后续 TTS 全被 play() 丢掉。
       */
      if (s_need_cancel.exchange(false, std::memory_order_acq_rel)) {
        finish_cancel();
      }
      continue;
    }
    if (job.type == JobType::kEndOfTask) {
      s_task_done.store(true, std::memory_order_release);
      continue;
    }
    execute_job(job);
  }
}

}  // namespace

void setup_speaker() {
  if (MAX98357_GAIN >= 0) {
    pinMode(MAX98357_GAIN, INPUT);
  }
  if (MAX98357_SD >= 0) {
    pinMode(MAX98357_SD, OUTPUT);
    digitalWrite(MAX98357_SD, HIGH);
  }
  if (!s_output_queue) s_output_queue = xQueueCreate(4, sizeof(OutputBlock));
  if (!s_mouth_queue) s_mouth_queue = xQueueCreate(1, sizeof(audio_lipsync::Frame));
  if (!s_output_queue || !s_mouth_queue) {
    log_error("[SPEAKER] output queue allocation failed"); return;
  }
  if (!s_tx) {
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
    channel.dma_desc_num = kDmaBufCount;
    channel.dma_frame_num = kDmaBufLen;
    channel.auto_clear_before_cb = true;
    // Application feeds DMA in on_sent; it does not mix driver writes with
    // direct-buffer ownership. Empty buffers always transmit zero.
    if (i2s_new_channel(&channel, &s_tx, nullptr) != ESP_OK) return;
    i2s_std_config_t config{};
    config.clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE);
    config.slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
    config.gpio_cfg.mclk = I2S_GPIO_UNUSED;
    config.gpio_cfg.bclk = static_cast<gpio_num_t>(MAX98357_BCLK);
    config.gpio_cfg.ws = static_cast<gpio_num_t>(MAX98357_LRC);
    config.gpio_cfg.dout = static_cast<gpio_num_t>(MAX98357_DIN);
    config.gpio_cfg.din = I2S_GPIO_UNUSED;
    i2s_event_callbacks_t callbacks{};
    callbacks.on_sent = on_output_sent;
    if (i2s_channel_init_std_mode(s_tx, &config) != ESP_OK ||
        i2s_channel_register_event_callback(s_tx, &callbacks, nullptr) != ESP_OK ||
        i2s_channel_enable(s_tx) != ESP_OK) {
      log_error("[SPEAKER] I2S initialization failed"); return;
    }
  }
  s_i2s_rate = SAMPLE_RATE;
  if (!s_queue) {
    s_queue = xQueueCreate(SPEAKER_QUEUE_DEPTH, sizeof(Job));
    if (!s_queue) {
      log_error("[SPEAKER] queue create failed");
      return;
    }
  }
  log_info("[SPEAKER] ready ESP_I2S DIN=%d vol=%.2f depth=%d", (int)MAX98357_DIN,
           (double)s_volume.load(std::memory_order_relaxed), (int)SPEAKER_QUEUE_DEPTH);
}

void task_setup_speaker() {
  if (s_task) {
    return;
  }
  if (!s_tx || !s_queue || !s_output_queue || !s_mouth_queue) {
    log_error("[SPEAKER] setup_speaker must succeed before task_setup_speaker"); return;
  }
  const BaseType_t rc = utils_task_create_pinned(task_loop_speaker, "speaker", kSpeakerTaskStack,
                                                 nullptr, 7, &s_task, APP_CPU_NUM);
  if (rc != pdPASS) {
    log_error("[SPEAKER] task create rc=%d", (int)rc);
  } else {
    log_info("[SPEAKER] task started stack=%u", (unsigned)kSpeakerTaskStack);
  }
}

void speaker_set_volume(int vol_0_100) {
  if (vol_0_100 < 0) {
    vol_0_100 = 0;
  } else if (vol_0_100 > 100) {
    vol_0_100 = 100;
  }
  s_volume.store(vol_0_100 / 100.0f, std::memory_order_release);
}

int speaker_get_volume(void) {
  return (int)(s_volume.load(std::memory_order_acquire) * 100.0f + 0.5f);
}

bool speaker_is_speaking() {
  return s_is_speaking.load(std::memory_order_acquire);
}

bool speaker_stream_pcm_active() {
  return s_stream_active.load(std::memory_order_acquire);
}

unsigned speaker_input_queue_depth() {
  return (unsigned)uxQueueMessagesWaiting(s_queue);
}

bool speaker_stream_pcm16_begin(uint32_t sample_rate, uint8_t channels) {
  if ((channels != 1 && channels != 2) || sample_rate == 0) {
    return false;
  }
  Job j{};
  j.type = JobType::kBegin;
  j.rate = sample_rate;
  j.channels = channels;
  return enqueue(j);
}

bool speaker_stream_pcm16_chunk(int16_t* samples, size_t num_samples,
                                uint32_t caps_for_heap_caps_free) {
  if (!samples || num_samples == 0) {
    return false;
  }
  Job j{};
  j.type = JobType::kChunk;
  j.pcm.ptr = samples;
  j.pcm.samples = num_samples;
  j.free_mode = caps_to_mode(caps_for_heap_caps_free);
  if (!enqueue(j)) {
    free_ptr(samples, j.free_mode);
    return false;
  }
  return true;
}

bool speaker_submit_pb_audio_owned(pb_audio* audio) {
  if (!audio) {
    return false;
  }
  Job j{};
  j.type = JobType::kPbAudio;
  j.pb_audio_ptr = audio;
  if (audio->epoch) s_buffered_ms.fetch_add(audio->duration_ms);
  if (!enqueue(j)) {
    if (audio->epoch) s_buffered_ms.fetch_sub(audio->duration_ms);
    pb_audio_free(audio);
    return false;
  }
  return true;
}

bool speaker_stream_pcm16_end(uint8_t channels) {
  if (channels != 1 && channels != 2) {
    return false;
  }
  Job j{};
  j.type = JobType::kEnd;
  j.channels = channels;
  return enqueue(j);
}

void speaker_abort() {
  Job j{};
  j.type = JobType::kCancel;
  s_cancel_in_progress.store(true, std::memory_order_release);
  s_need_cancel.store(true, std::memory_order_release);
  s_output_generation.fetch_add(1, std::memory_order_release);
  (void)xQueueSend(s_queue, &j, portMAX_DELAY);
}

bool speaker_task_done() {
  return s_task_done.load(std::memory_order_acquire);
}

void speaker_signal_task_done() {
  Job j{};
  j.type = JobType::kEndOfTask;
  (void)xQueueSend(s_queue, &j, portMAX_DELAY);
}

void speaker_set_task_done_flag() {
  s_task_done.store(true, std::memory_order_release);
}

bool speaker_play_url(const char* url) {
  uint8_t* buf = nullptr;
  size_t len = 0;
  if (!utils_http_get_binary(url, &buf, &len)) {
    return false;
  }
  if (len < 44) {
    log_error("[SPEAKER] body too short for WAV (%u)", (unsigned)len);
    heap_caps_free(buf);
    return false;
  }
  Job j{};
  j.type = JobType::kWav;
  j.wav.ptr = buf;
  j.wav.len = len;
  j.free_mode = HeapFree::kHeapCaps;
  if (!enqueue(j)) {
    heap_caps_free(buf);
    return false;
  }
  return true;
}

uint32_t speaker_buffered_ms() { return s_buffered_ms.load(std::memory_order_acquire); }
int32_t speaker_timeline_ms(uint32_t epoch) {
  if (s_timeline_epoch.load(std::memory_order_acquire) != epoch) return -1;
  return (int32_t)s_timeline_ms.load(std::memory_order_acquire);
}
bool speaker_cancel_pending() { return s_need_cancel.load(std::memory_order_acquire) || s_cancel_in_progress.load(std::memory_order_acquire); }

// Snapshot is published only when its DMA buffer actually completed output.
// Stop/cancel invalidates every old-generation frame, including an ISR race.
audio_lipsync::Frame speaker_mouth_frame() {
  audio_lipsync::Frame frame;
  if (xQueuePeek(s_mouth_queue, &frame, 0) != pdTRUE ||
      frame.generation != s_output_generation.load(std::memory_order_acquire) ||
      s_cancel_in_progress.load(std::memory_order_acquire)) return {};
  return frame;
}
String speaker_status_json() {
  JsonDocument doc;doc["type"]="audio_status";doc["volume"]=speaker_get_volume();
  doc["generation"]=s_output_generation.load();doc["playing"]=speaker_is_speaking();
  doc["afe_ready"]=audio_frontend_ready();doc["afe_healthy"]=audio_frontend_output_healthy();
  doc["content_gap_periods"]=s_content_gap_periods.load();doc["content_gap_events"]=s_content_gap_events.load();
  doc["output_empty_periods"]=s_output_empty_periods.load();doc["sample_rate"]=s_i2s_rate;
  doc["buffered_ms"]=speaker_buffered_ms();String out;serializeJson(doc,out);return out;
}
