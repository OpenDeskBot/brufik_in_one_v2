#pragma once
#include <cstddef>
#include <cstdint>

// 16k -> 8k diagnostic copy only. Playback and ESP-SR retain full-rate PCM.
// 32-tap Hamming low-pass, 3400 Hz cutoff; Q15, ~0.97 ms group delay.
namespace usb_trace {
class Decimator {
  int16_t history_[32]{};
  unsigned at_=0;
 public:
  void process(const int16_t* input, size_t samples, int16_t* output) {
    static constexpr int16_t coefficients[32] = {52, 31, -66, -112, 70, 292, 49, -540, -434, 712, 1226, -516, -2642, -765, 6169, 12857, 12859, 6169, -765, -2642, -516, 1226, 712, -434, -540, 49, 292, 70, -112, -66, 31, 52};
    size_t written=0;
    for(size_t i=0;i<samples;++i) {
      history_[at_]=input[i]; at_=(at_+1)%32;
      if(i%2) {
        int64_t total=0;
        for(unsigned j=0;j<32;++j) total+=static_cast<int32_t>(history_[(at_+31-j)%32])*coefficients[j];
        int32_t value=total>>15;
        if(value>32767) value=32767; if(value< -32768) value=-32768;
        output[written++]=static_cast<int16_t>(value);
      }
    }
  }
};
}
