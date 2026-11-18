#include "face_assets.h"
#include <cstring>
#include <esp_heap_caps.h>
#ifdef ARDUINO_ARCH_ESP32
#include "face_package.h"
#endif

namespace {
// Compact immutable Flash representation. Text is pooled as constant strings.
struct FacePrimitive {
  pb_anim_element_layer layer;
  pb_anim_element_shape shape;
  uint16_t color;
  int16_t x, y, w, h, r, x1, y1, x2, y2;
  uint8_t text_size;
  const char* text;
};
struct FaceFrame { uint32_t offset; uint16_t count, ms; };
struct FaceEntry { const char* id; uint16_t offset, count; uint32_t duration; };
#include "generated/face_assets.inc"
}  // namespace

const char* face_assets_version() {
#ifdef ARDUINO_ARCH_ESP32
  if (*face_package_version()) return face_package_version();
#endif
  return kFaceAssetsVersion;
}
const char* face_assets_mouth_phoneme(bool open) {
#ifdef ARDUINO_ARCH_ESP32
  if (*face_package_version()) return face_package_mouth(open);
#endif
  return open?kFaceOpenPhoneme:kFaceClosedPhoneme;
}
#ifdef ARDUINO_ARCH_ESP32
JsonObjectConst face_assets_idle() {
  if (*face_package_version()) return face_package_idle();
  static JsonDocument s_idle;
  if (s_idle.isNull()) deserializeJson(s_idle,kFaceIdleJson);
  return s_idle.as<JsonObjectConst>();
}
#endif

static bool load_entry(const char* id, const FaceEntry* table, size_t table_count,
                      bool mouth_only, uint32_t requested_ms, pb_anim_frame*& frames,
                      size_t& count, uint32_t& duration_ms, const char*& error,
                      int selected_frame = -1) {
  frames = nullptr;
  count = 0;
  duration_ms = 0;
  error = nullptr;
  const FaceEntry* entry = nullptr;
  if (id) {
    for (size_t i = 0; i < table_count; ++i) {
      const auto& candidate = table[i];
      if (strcmp(id, candidate.id) == 0) { entry = &candidate; break; }
    }
  }
  if (!entry) { error = mouth_only ? "unknown phoneme_id" : "unknown anim_id"; return false; }
  FaceEntry selected = *entry;
  if (selected_frame >= 0) {
    if (static_cast<unsigned>(selected_frame) >= entry->count) {
      error = "invalid animation frame"; return false;
    }
    selected.offset += selected_frame;
    selected.count = 1;
    selected.duration = kFaceFrames[selected.offset].ms;
    entry = &selected;
  }
  if (mouth_only && (requested_ms < entry->count || requested_ms > 30000)) {
    error = "invalid phoneme duration"; return false;
  }
  auto* result = static_cast<pb_anim_frame*>(heap_caps_calloc(
      entry->count, sizeof(pb_anim_frame), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!result) { error = "expression frames allocation failed"; return false; }
  uint32_t source_ms = 0, allocated_ms = 0;
  for (size_t i = 0; i < entry->count; ++i) {
    const auto& src = kFaceFrames[entry->offset + i];
    auto& dst = result[i];
    dst.ms = src.ms;
    source_ms += src.ms;
    if (mouth_only) {
      // Reserve one millisecond for every subframe, then divide the remainder by weight.
      const uint32_t end = i + 1 + (uint64_t)source_ms * (requested_ms-entry->count) / entry->duration;
      dst.ms = end - allocated_ms;
      allocated_ms = end;
      strncpy(dst.phoneme, id, sizeof(dst.phoneme)-1);
    }
    size_t element_count = 0;
    for (size_t j = 0; j < src.count; ++j)
      if (!mouth_only || kFacePrimitives[src.offset+j].layer == pb_anim_element_layer::mouth) ++element_count;
    dst.elements = static_cast<pb_anim_element*>(heap_caps_calloc(
        element_count, sizeof(pb_anim_element), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!dst.elements) {
      pb_anim_frames_free(result, entry->count);
      error = "expression primitives allocation failed";
      return false;
    }
    for (size_t j = 0; j < src.count; ++j) {
      const auto& p = kFacePrimitives[src.offset + j];
      if (mouth_only && p.layer != pb_anim_element_layer::mouth) continue;
      auto& out = dst.elements[dst.element_count++];
      out.layer = p.layer; out.shape = p.shape; out.color = p.color;
      out.x = p.x; out.y = p.y; out.w = p.w; out.h = p.h; out.r = p.r;
      out.x1 = p.x1; out.y1 = p.y1; out.x2 = p.x2; out.y2 = p.y2;
      out.text_size = p.text_size;
      if (p.text) strncpy(out.text, p.text, sizeof(out.text) - 1);
    }
  }
  frames = result;
  count = entry->count;
  duration_ms = mouth_only ? requested_ms : entry->duration;
  return true;
}

bool face_assets_load(const char* id, pb_anim_frame*& frames, size_t& count,
                      uint32_t& duration_ms, const char*& error) {
#ifdef ARDUINO_ARCH_ESP32
  if (*face_package_version()) return face_package_load(id,false,0,-1,frames,count,duration_ms,error);
#endif
  return load_entry(id, kFaceEntries, sizeof(kFaceEntries)/sizeof(*kFaceEntries),
                    false, 0, frames, count, duration_ms, error);
}

bool face_assets_load_phoneme(const char* id, uint32_t duration_ms,
                             pb_anim_frame*& frames, size_t& count, const char*& error) {
#ifdef ARDUINO_ARCH_ESP32
  if (*face_package_version()) { uint32_t actual=0;return face_package_load(id,true,duration_ms,-1,frames,count,actual,error); }
#endif
  uint32_t actual_ms = 0;
  return load_entry(id, kPhonemeEntries, sizeof(kPhonemeEntries)/sizeof(*kPhonemeEntries),
                    true, duration_ms, frames, count, actual_ms, error);
}

bool face_assets_load_frame(const char* id, unsigned index, uint32_t ms,
                           pb_anim_frame*& frames, size_t& count, const char*& error) {
  frames = nullptr; count = 0;
  if (index >= PB_ANIM_FRAME_CAPACITY || !ms || ms > 30000) {
    error = "invalid animation frame or duration"; return false;
  }
  uint32_t duration = 0;
#ifdef ARDUINO_ARCH_ESP32
  if (*face_package_version()) return face_package_load(id,false,ms,index,frames,count,duration,error);
#endif
  if (!load_entry(id, kFaceEntries, sizeof(kFaceEntries)/sizeof(*kFaceEntries),
                  false, 0, frames, count, duration, error, index)) return false;
  frames[0].ms = ms;
  return true;
}
