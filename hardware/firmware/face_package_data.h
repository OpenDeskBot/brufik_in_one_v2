#pragma once
#include <ArduinoJson.h>
#include <cstring>

// Portable validation, shared by firmware and native malformed-package tests.
namespace face_package_data {
constexpr size_t kMaxBytes = 256 * 1024;
constexpr const char* kLayers[] = {"bg","nose","mouth","eye_l","eye_r","extra"};
constexpr const char* kShapes[] = {"none","rect","rect_outline","circle","circle_outline","line",
    "ellipse","ellipse_fill","round_rect","round_rect_outline","text"};
inline int name_index(const char* value, const char* const* table, unsigned count) {
  if (value) for (unsigned i=0;i<count;++i) if (!strcmp(value,table[i])) return i;
  return -1;
}
inline bool integer(JsonVariantConst v, int lo, int hi) {
  return v.is<int>() && !v.is<bool>() && v.as<int>()>=lo && v.as<int>()<=hi;
}
inline bool text(JsonVariantConst v, size_t max) {
  return v.is<const char*>() && v.as<JsonString>().size()>0 && v.as<JsonString>().size()<=max &&
      strlen(v.as<const char*>())==v.as<JsonString>().size();
}
inline JsonObjectConst find(JsonArrayConst entries, const char* name) {
  if (!name) return {};
  for (JsonObjectConst entry: entries) {
    if (!strcmp(entry["id"] | "",name)) return entry;
    for (const char* alias:entry["aliases"].as<JsonArrayConst>()) if (!strcmp(alias,name)) return entry;
  }
  return {};
}
inline bool validate(JsonVariantConst root) {
  if (strcmp(root["format"] | "","deskbot-face-v1")) return false;
  auto lib=root["library"];
  if (lib["schema"] != 2 || !text(lib["version"],16) || strlen(lib["version"].as<const char*>())!=16) return false;
  for (const char* p=lib["version"].as<const char*>();*p;++p)
    if (!(*p>='0'&&*p<='9') && !(*p>='a'&&*p<='f')) return false;
  auto primitives=lib["primitives"].as<JsonArrayConst>();
  auto frames=lib["frames"].as<JsonArrayConst>();
  if (!primitives.size() || primitives.size()>4096 || !frames.size() || frames.size()>1024) return false;
  for (JsonArrayConst p:primitives) {
    if (p.size()!=14 || name_index(p[0],kLayers,6)<0 || name_index(p[1],kShapes,11)<=0 ||
        !integer(p[2],0,65535) || !integer(p[12],1,3) || !p[13].is<const char*>()) return false;
    if (p[13].as<JsonString>().size()>128 || strlen(p[13].as<const char*>())!=p[13].as<JsonString>().size()) return false;
    for (unsigned i=3;i<12;++i) if (!integer(p[i],-1024,1024)) return false;
  }
  for (JsonArrayConst f:frames) {
    if (f.size()!=3 || !integer(f[0],0,primitives.size()-1) || !integer(f[1],1,96) ||
        f[0].as<unsigned>()+f[1].as<unsigned>()>primitives.size() || !integer(f[2],40,10000)) return false;
    unsigned layers[6]{};
    for (unsigned i=f[0]; i<f[0].as<unsigned>()+f[1].as<unsigned>();++i)
      if (++layers[name_index(primitives[i][0],kLayers,6)]>16) return false;
  }
  for (const char* kind:{"assets","phonemes"}) {
    auto entries=lib[kind].as<JsonArrayConst>();
    if (!entries.size() || entries.size()>128) return false;
    for (JsonObjectConst a:entries) {
      if (!text(a["id"],!strcmp(kind,"assets")?48:15) || !a["aliases"].is<JsonArrayConst>() ||
          a["aliases"].size()>32 || !integer(a["frame_offset"],0,frames.size()-1) ||
          !integer(a["frame_count"],1,64) || a["frame_offset"].as<unsigned>()+a["frame_count"].as<unsigned>()>frames.size()) return false;
      for (JsonVariantConst alias:a["aliases"].as<JsonArrayConst>()) if (!text(alias,!strcmp(kind,"assets")?48:15)) return false;
      unsigned duration=0;
      for (unsigned i=0;i<a["frame_count"].as<unsigned>();++i) {
        auto f=frames[a["frame_offset"].as<unsigned>()+i]; duration+=f[2].as<unsigned>();
        bool mouth=false;
        for (unsigned j=f[0];j<f[0].as<unsigned>()+f[1].as<unsigned>();++j)
          mouth |= !strcmp(primitives[j][0],"mouth");
        if (!mouth) return false;
      }
      if (!integer(a["duration_ms"],1,10000) || duration!=a["duration_ms"].as<unsigned>()) return false;
    }
    // All names have been type checked before comparing any cross-references.
    for (unsigned i=0;i<entries.size();++i) {
      auto a=entries[i];
      for (unsigned x=0;x<a["aliases"].size();++x) {
        const char* alias=a["aliases"][x];
        if (!strcmp(alias,a["id"])) return false;
        for (unsigned y=0;y<x;++y) if (!strcmp(alias,a["aliases"][y])) return false;
      }
      for (unsigned j=0;j<i;++j) {
        auto b=entries[j];
        if (!strcmp(a["id"],b["id"])) return false;
        for (const char* alias:b["aliases"].as<JsonArrayConst>()) if (!strcmp(a["id"],alias)) return false;
        for (const char* alias:a["aliases"].as<JsonArrayConst>()) {
          if (!strcmp(alias,b["id"])) return false;
          for (const char* other:b["aliases"].as<JsonArrayConst>()) if (!strcmp(alias,other)) return false;
        }
      }
    }
  }
  auto lips=lib["lipsync"];
  // Older cached/downloaded packs may still contain MFCC metadata. Accept their
  // two mouth endpoints, but never require or interpret voice/vowel profiles.
  if (!lips.is<JsonObjectConst>() || !text(lips["classifier"],32) ||
      (strcmp(lips["classifier"],"rms-v1") && strcmp(lips["classifier"],"mfcc-vowel-v1")) ||
      !text(lips["closed_phoneme"],15) || !text(lips["open_phoneme"],15) ||
      find(lib["phonemes"],lips["closed_phoneme"]).isNull() ||
      find(lib["phonemes"],lips["open_phoneme"]).isNull()) return false;
  auto idle=lib["idle"];
  if (!integer(idle["after_ms"],1000,3600000) || !integer(idle["repeat_ms"],1000,3600000) ||
      !idle["clips"].is<JsonArrayConst>() || !idle["clips"].size() || idle["clips"].size()>16) return false;
  for (JsonObjectConst clip:idle["clips"].as<JsonArrayConst>()) {
    if (find(lib["assets"],clip["anim_id"]).isNull() || !clip["servo"].is<JsonArrayConst>() || clip["servo"].size()>16) return false;
    unsigned duration=0;
    for (JsonObjectConst step:clip["servo"].as<JsonArrayConst>()) {
      if (!integer(step["xm"],0,2) || !integer(step["ym"],0,2) || !integer(step["ms"],100,3000)) return false;
      for (const char* axis:{"x","y"}) {
        const bool relative=step[axis[0]=='x'?"xm":"ym"]==1;
        if (!integer(step[axis],relative?-30:0,relative?30:180)) return false;
      }
      duration+=step["ms"].as<unsigned>();
    }
    if (duration>10000) return false;
  }
  return true;
}
} // namespace face_package_data
