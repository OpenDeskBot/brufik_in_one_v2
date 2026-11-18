#pragma once
#include <cmath>
#include <cstring>
#include <cstdint>

// Pure transition math shared by display sampling and native regression tests.
namespace face_transition {
inline int coordinate(int a, int b, float t) {
  return static_cast<int>(std::lround((1.f-t)*a+t*b));
}
inline uint16_t color(uint16_t a, uint16_t b, float t) {
  return (coordinate((a>>11)&31,(b>>11)&31,t)<<11) |
         (coordinate((a>>5)&63,(b>>5)&63,t)<<5) |
         coordinate(a&31,b&31,t);
}

template<class Prim>
Prim primitive(const Prim* from, const Prim* to, float t) {
  if (!from) { Prim p=*to; p.color=color(0,p.color,t); return p; }
  if (!to) { Prim p=*from; p.color=color(p.color,0,t); return p; }
  if (t<=0) return *from;
  if (t>=1) return *to;
  if (from->shape != to->shape || from->text_size != to->text_size ||
      std::strcmp(from->text,to->text)!=0) {
    // Incompatible shapes/text disappear before their replacement appears.
    Prim p = t<.5f ? *from : *to;
    p.color = t<.5f ? color(p.color,0,t*2) : color(0,p.color,(t-.5f)*2);
    return p;
  }
  Prim p=*to;
  p.color=color(from->color,to->color,t);
  p.x=coordinate(from->x,to->x,t); p.y=coordinate(from->y,to->y,t);
  p.w=coordinate(from->w,to->w,t); p.h=coordinate(from->h,to->h,t);
  p.r=coordinate(from->r,to->r,t);
  p.x1=coordinate(from->x1,to->x1,t); p.y1=coordinate(from->y1,to->y1,t);
  p.x2=coordinate(from->x2,to->x2,t); p.y2=coordinate(from->y2,to->y2,t);
  return p;
}

template<class Layer>
void layer(const Layer* from, const Layer& to, float t, Layer& shown) {
  // Empty layer means an incremental update: phonemes leave eyes/decorations alone.
  if (!to.count) { shown=from ? *from : to; return; }
  if (t>=1) { shown=to; return; }
  const unsigned old_count=from ? from->count : 0;
  shown.count=old_count>to.count ? old_count : to.count;
  for (unsigned i=0;i<shown.count;++i)
    shown.prims[i]=primitive(i<old_count ? &from->prims[i] : nullptr,
                            i<to.count ? &to.prims[i] : nullptr,t);
}
}  // namespace face_transition
