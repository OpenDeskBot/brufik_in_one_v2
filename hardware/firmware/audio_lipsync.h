#pragma once
#include <cmath>
#include <cstddef>
#include <cstdint>

// Volume-only mouth animation. Pure math, also exercised on the host.
namespace audio_lipsync {
struct Frame {
  uint32_t generation = 0, epoch = 0, end_ms = 0;
  uint64_t first_sample = 0, output_us = 0;
  float rms = 0, open = 0;
  bool active = false;
  bool underrun = false;  // Actual DMA output was zero because no PCM was ready.
};
constexpr size_t kBlockFrames = 160;
struct PcmBlock {
  int16_t pcm[2 * kBlockFrames]{};
  Frame lip;
  size_t frames = 0;
  // Append only new source frames; retain a partial period across calls.
  size_t append(const int16_t* source, size_t count, unsigned channels) {
    if (channels != 1 && channels != 2) return 0;
    const size_t room = kBlockFrames-frames;
    const size_t n = count<room ? count : room;
    for (size_t i=0; i<n; ++i) {
      const int32_t value = channels == 1 ? source[i] :
          (static_cast<int32_t>(source[2*i])+source[2*i+1])/2;
      pcm[2*(frames+i)] = pcm[2*(frames+i)+1] = static_cast<int16_t>(value);
    }
    frames += n;
    return n;
  }
};
inline Frame process_audio(const int16_t* pcm, size_t frames, unsigned channels,
                           float silence_db, float full_db) {
  Frame out;
  if (!frames || (channels != 1 && channels != 2)) return out;
  float energy = 0;
  for (size_t i = 0; i < frames; ++i) {
    // Physical amplifier selects one slot; the speaker sends the same mono
    // content to both slots, including when the input was stereo.
    const float x = pcm[i * channels] / 32768.f;
    energy += x * x;
  }
  out.rms = std::sqrt(energy / frames);
  const float db = 20.f * std::log10(out.rms > 1e-12f ? out.rms : 1e-12f);
  if (db > silence_db) {
    out.open = (db - silence_db) / (full_db - silence_db);
    if (out.open > 1.f) out.open = 1.f;
  }
  out.active = true;
  return out;
}
inline float smooth(float current, float target, float dt_ms) {
  const float tau = target > current ? 35.f : 70.f;
  return current + (target-current) * (1.f-std::exp(-dt_ms/tau));
}

// Owned by the display task. Both network and USB playback use this same ROM
// animation path; the host never supplies mouth envelopes or transition state.
struct AnimationState {
  uint32_t generation = 0;
  float open = 0;
};
inline void process_animation(const Frame& frame, uint32_t elapsed_ms,
                              AnimationState& state) {
  if (!frame.active || frame.underrun) {
    state = AnimationState{};
    return;
  }
  if (state.generation != frame.generation) {
    state = AnimationState{};
    state.generation = frame.generation;
    // Idle time and an old utterance's pose must not open a new utterance.
    if (elapsed_ms > 10) elapsed_ms = 10;
  }
  state.open = smooth(state.open, frame.open, elapsed_ms);
  if (state.open < .001f) state.open = 0.f;
}
}  // namespace audio_lipsync
