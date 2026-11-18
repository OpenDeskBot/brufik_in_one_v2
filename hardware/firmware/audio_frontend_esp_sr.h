#pragma once

#include <stddef.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"

/*
 * Official Espressif ESP-SR audio front end used by the USB/RTC uplink.
 *
 * Input is one 16 kHz microphone channel plus one 16 kHz playback-reference
 * channel ("MR").  The output is always mono PCM16 at 16 kHz after AEC, NS,
 * AGC and VAD processing.
 */

static constexpr size_t kAudioFrontendIoFrameSamples = 320;  // 20 ms @ 16 kHz

enum class AudioFrontendVadState : uint8_t {
  kSilence = 0,
  kSpeech = 1,
};

struct AudioFrontendVadEvent {
  AudioFrontendVadState state = AudioFrontendVadState::kSilence;
  uint32_t sequence = 0;
  int16_t volume_db_x100 = 0;
};

/** Initialise the ESP-SR AFE. Failure is non-fatal and leaves raw-mic fallback. */
bool audio_frontend_setup();

/** True only after AEC, NS and VAD were all created and enabled successfully. */
bool audio_frontend_ready();

/** Processed PCM is healthy (as distinct from an allocated/running AFE). */
bool audio_frontend_output_healthy();

/** Speaker reference or its acoustic tail is still present. */
bool audio_frontend_playback_or_tail_active();

/**
 * Degrade the uplink to the continuously retained raw microphone stream.
 *
 * The ESP-SR workers remain alive so a later sequence of healthy processed
 * frames can restore AEC/NS automatically.  This is intentionally safe to
 * call from the independent runtime supervisor.
 */
void audio_frontend_force_raw_fallback(const char* reason);

/** Queue one raw 20 ms microphone frame for AFE processing. Never blocks. */
bool audio_frontend_submit_mic(const int16_t* pcm, size_t samples);

/** Read one processed 20 ms frame. */
bool audio_frontend_take_output_frame(int16_t* pcm, TickType_t ticks_to_wait);

/**
 * Track the audible playback lifetime.  A transition to active starts a fresh
 * playback-reference timeline; inactive lets its acoustic tail drain.
 */
void audio_frontend_set_playback_active(bool active);

/**
 * Immediately discard a reference timeline after an I2S write/recovery
 * failure.  Normal playback end uses set_playback_active(false) so the
 * acoustic tail can still drain; this fail-closed path must not feed samples
 * that never reached the speaker.
 */
void audio_frontend_abort_playback_reference();

/**
 * Queue samples immediately before the matching speaker I2S write. `samples`
 * is the total interleaved sample count. Arbitrary rates/channels are
 * converted to 16 kHz mono before entering the AEC reference delay line.
 */
void audio_frontend_submit_playback_reference(
    const int16_t* pcm,
    size_t samples,
    uint32_t sample_rate,
    uint8_t channels);

/** Consume a debounced ESP-SR VAD transition for USB side-band signalling. */
bool audio_frontend_take_vad_event(AudioFrontendVadEvent* event);

/** Non-consuming, debounced speech state for local attention/idle control.
 * Callers use ready/output_healthy to distinguish silence from unavailable VAD.
 */
bool audio_frontend_speech_active();

/** Idle-only bench calibration, 0..160 ms; takes effect on the next stream. */
bool audio_frontend_set_reference_delay_ms(uint32_t ms);
uint32_t audio_frontend_reference_delay_ms();

/** USB bench only: A/B comparison; applied by the AFE feed owner at a frame boundary. */
bool audio_frontend_bench_set_aec_enabled(bool enabled);
bool audio_frontend_bench_aec_enabled();
