#pragma once
#include <stddef.h>
#include <stdint.h>
namespace rom_control {
enum class VolumeAction { Get, Set, Adjust };
inline bool volume_target(int current,int value,VolumeAction action,int& result) {
  if (current<0 || current>100 || value<-100 || value>100) return false;
  if (action==VolumeAction::Get) {result=current;return true;}
  if (action==VolumeAction::Set && value<0) return false;
  const int wanted=action==VolumeAction::Set?value:current+value;
  result=wanted<0?0:(wanted>100?100:wanted);return true;
}
inline bool image_size(unsigned w,unsigned h,size_t bytes,unsigned wanted_w,unsigned wanted_h) {
  return w==wanted_w && h==wanted_h && w>0 && h>0 && bytes==(size_t)w*h*sizeof(uint16_t);
}
}
