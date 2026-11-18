#pragma once
#include <cstdint>

// Monotonic, wrap-safe inactivity policy; the PB owner performs all dispatch.
struct LocalIdlePolicy {
  uint32_t last_command=0,last_idle=0,last_speech=0;
  bool open_response=false;
  bool detector_ready=false,user_speaking=false,heard_speech=false;
  void command(uint32_t now){last_command=now;}
  void observe_speech(uint32_t now,bool ready,bool speaking) {
    detector_ready=ready;
    user_speaking=ready && speaking;
    if(user_speaking){last_speech=now;heard_speech=true;}
  }
  // Without usable VAD, don't start movements that cannot yield to speech.
  bool stop_for_attention() const {return !detector_ready || user_speaking;}
  bool due(uint32_t now,uint32_t after,uint32_t repeat,bool busy) const {
    return !stop_for_attention() && !open_response && !busy &&
        now-last_command>=after && now-last_idle>=repeat &&
        (!heard_speech || now-last_speech>=after);
  }
};
