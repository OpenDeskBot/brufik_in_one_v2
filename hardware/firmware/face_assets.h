#pragma once
#include "pb_model.h"

// Active downloaded/cache/fallback content version (not the firmware release).
const char* face_assets_version();
const char* face_assets_mouth_phoneme(bool open);
#ifdef ARDUINO_ARCH_ESP32
// Borrowed immutable idle program; callers copy configuration before starting tasks.
JsonObjectConst face_assets_idle();
#endif
// ID/alias lookup. Allocates only the selected animation in PSRAM; caller owns it.
// On failure outputs remain empty. Free via pb_anim_frames_free, never free Flash tables.
bool face_assets_load(const char* id, pb_anim_frame*& frames, size_t& count,
                      uint32_t& duration_ms, const char*& error);

// Select a local keyframe for a timed audio overlay; never restart the full clip.
bool face_assets_load_frame(const char* id, unsigned index, uint32_t ms,
                           pb_anim_frame*& frames, size_t& count, const char*& error);

// Lookup mouth-only animation; scale authored subframes to the requested speech duration.
bool face_assets_load_phoneme(const char* id, uint32_t duration_ms,
                             pb_anim_frame*& frames, size_t& count, const char*& error);
