#pragma once
// Portable envelope codec: no packed structs, alignment assumptions or heap IO.
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace media_wire {
constexpr size_t kAudioHeader = 24, kAckHeader = 32;
constexpr size_t kMaxMedia = 256 * 1024 - kAudioHeader;
inline uint32_t read32(const uint8_t* p) {
  return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | p[3];
}
inline uint16_t read16(const uint8_t* p) { return uint16_t((p[0] << 8) | p[1]); }
inline void write32(uint8_t* p, uint32_t x) {
  p[0]=x>>24; p[1]=x>>16; p[2]=x>>8; p[3]=x;
}
inline void write16(uint8_t* p, uint16_t x) { p[0]=x>>8; p[1]=x; }
inline bool magic(const uint8_t* p, size_t n) { return n>=2 && p[0]=='D' && p[1]=='B'; }
inline void prefix(uint8_t* p, uint8_t kind) { p[0]='D'; p[1]='B'; p[2]=1; p[3]=kind; }
struct Audio {
  uint32_t epoch=0, idx=0, pts=0, bytes=0;
  uint16_t duration=0, frames=0;
};
inline void audio_encode(uint8_t* p, uint8_t kind, const Audio& a) {
  prefix(p,kind); write32(p+4,a.epoch); write32(p+8,a.idx); write32(p+12,a.pts);
  write16(p+16,a.duration); write16(p+18,a.frames); write32(p+20,a.bytes);
}
inline bool audio_decode(const uint8_t* p, size_t n, Audio& a) {
  if (n<kAudioHeader || !magic(p,n) || p[2]!=1 || p[3]!=1) return false;
  a.epoch=read32(p+4); a.idx=read32(p+8); a.pts=read32(p+12);
  a.duration=read16(p+16); a.frames=read16(p+18); a.bytes=read32(p+20);
  return a.epoch && a.idx && a.idx<=0x7fffffff && a.duration && a.bytes &&
         a.bytes<=kMaxMedia && n==kAudioHeader+a.bytes;
}
inline int ack_code(const char* kind) {
  const char* names[]={"pb_chunk","pb_end","pb_credit","pb_audio_end","pb_started",
                       "pb_progress","pb_cancelled","pb_error"};
  for (int i=0;i<8;++i) if (strcmp(kind,names[i])==0) return i;
  return -1;
}
inline void ack_encode(uint8_t* p, uint32_t epoch, uint32_t idx, uint32_t played,
                       uint32_t buffered, uint32_t device, uint16_t space,
                       uint8_t code, bool progress, uint32_t wait) {
  prefix(p,3); write32(p+4,epoch); write32(p+8,idx); write32(p+12,played);
  write32(p+16,buffered); write32(p+20,device); write16(p+24,space);
  p[26]=code; p[27]=progress ? 1 : 0; write32(p+28,wait);
}
}  // namespace media_wire
