#include "face_package.h"
#include "face_package_data.h"
#include <esp_heap_caps.h>

namespace {
struct PsramAllocator: ArduinoJson::Allocator {
  void* allocate(size_t n) override { return heap_caps_malloc(n,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT); }
  void deallocate(void* p) override { heap_caps_free(p); }
  void* reallocate(void* p,size_t n) override { return heap_caps_realloc(p,n,MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT); }
} s_allocator;
JsonDocument s_document(&s_allocator);
bool decode(JsonDocument& candidate,const uint8_t* bytes,size_t length,const char* expected_version) {
  return bytes && length && length<=face_package_data::kMaxBytes &&
      !deserializeJson(candidate,bytes,length,DeserializationOption::NestingLimit(12)) &&
      face_package_data::validate(candidate.as<JsonVariantConst>()) &&
      (!expected_version || !strcmp(candidate["library"]["version"] | "",expected_version));
}
}
bool face_package_install(const uint8_t* bytes,size_t length,const char* expected_version) {
  JsonDocument candidate(&s_allocator);
  if (!decode(candidate,bytes,length,expected_version)) return false;
  s_document=std::move(candidate);
  return true;
}
bool face_package_validate(const uint8_t* bytes,size_t length,const char* expected_version) {
  JsonDocument candidate(&s_allocator);
  return decode(candidate,bytes,length,expected_version);
}
const char* face_package_version() { return s_document["library"]["version"] | ""; }
const char* face_package_mouth(bool open) {
  return s_document["library"]["lipsync"][open?"open_phoneme":"closed_phoneme"] | "";
}
JsonObjectConst face_package_idle() { return s_document["library"]["idle"].as<JsonObjectConst>(); }

bool face_package_load(const char* id,bool mouth,uint32_t requested,int selected,
                       pb_anim_frame*& out,size_t& count,uint32_t& duration,const char*& error) {
  out=nullptr;count=0;duration=0;error=nullptr;
  auto lib=s_document["library"].as<JsonObjectConst>();
  auto entry=face_package_data::find(lib[mouth?"phonemes":"assets"],id);
  if (entry.isNull()) { error="unknown package resource ID";return false; }
  if (selected < -1 || (selected>=0 && (!requested || requested>30000))) {
    error="invalid package frame duration";return false;
  }
  unsigned n=entry["frame_count"],start=entry["frame_offset"];
  if (selected>=0) {
    if ((unsigned)selected>=n) {error="invalid package frame";return false;}
    start+=selected;n=1;
  }
  if (mouth && (requested<n || requested>30000)) {error="invalid mouth duration";return false;}
  auto* frames=(pb_anim_frame*)heap_caps_calloc(n,sizeof(pb_anim_frame),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
  if (!frames) {error="package frame allocation failed";return false;}
  unsigned elapsed=0,allocated=0,actual_duration=0;
  for (unsigned i=0;i<n;++i) {
    auto f=lib["frames"][start+i];auto& dst=frames[i];
    elapsed+=f[2].as<unsigned>(); dst.ms=f[2];
    if (mouth) {
      const unsigned end=i+1+(uint64_t)elapsed*(requested-n)/entry["duration_ms"].as<unsigned>();
      dst.ms=end-allocated;allocated=end;strncpy(dst.phoneme,id,sizeof(dst.phoneme)-1);
    } else if (selected>=0) dst.ms=requested;
    const unsigned offset=f[0],size=f[1];
    dst.elements=(pb_anim_element*)heap_caps_calloc(size,sizeof(pb_anim_element),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
    if (!dst.elements) {pb_anim_frames_free(frames,n);error="package primitive allocation failed";return false;}
    for (unsigned j=0;j<size;++j) {
      auto p=lib["primitives"][offset+j];
      const int layer=face_package_data::name_index(p[0],face_package_data::kLayers,6);
      if (mouth && layer!=2) continue;
      auto& e=dst.elements[dst.element_count++];
      e.layer=(pb_anim_element_layer)layer;
      e.shape=(pb_anim_element_shape)face_package_data::name_index(p[1],face_package_data::kShapes,11);
      e.color=p[2];e.x=p[3];e.y=p[4];e.w=p[5];e.h=p[6];e.r=p[7];
      e.x1=p[8];e.y1=p[9];e.x2=p[10];e.y2=p[11];
      e.text_size=p[12];strncpy(e.text,p[13] | "",sizeof(e.text)-1);
    }
    actual_duration+=dst.ms;
  }
  out=frames;count=n;duration=actual_duration;return true;
}
