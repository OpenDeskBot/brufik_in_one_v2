"""Native PCM continuity, volume-only animation, and actual speaker DMA ownership tests."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FW = Path(__file__).resolve().parents[1] / 'firmware'


@unittest.skipUnless(shutil.which('c++'), 'native C++ compiler unavailable')
class AudioLipSyncTest(unittest.TestCase):
    def compile_run(self, code):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'test.cpp'
            source.write_text(code)
            binary = Path(tmp) / 'test'
            subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-I'+str(FW), str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_pcm_chunking_stereo_silence_clipping_and_smoothing(self):
        self.compile_run(r'''
#include "audio_lipsync.h"
#include <cassert>
#include <vector>
using namespace audio_lipsync;
std::vector<int16_t> run(const std::vector<int16_t>& src, unsigned chunk,
                        std::vector<float>& openings) {
  PcmBlock block;
  std::vector<int16_t> result;
  auto flush = [&] {
    auto lip=process_audio(block.pcm,block.frames,2,-45,-15);
    assert(std::isfinite(lip.rms) && std::isfinite(lip.open));
    openings.push_back(lip.open);
    for (size_t i=0;i<block.frames;++i) {
      assert(block.pcm[2*i]==block.pcm[2*i+1]);
      result.push_back(block.pcm[2*i]);
    }
    block=PcmBlock{};
  };
  for (size_t off=0;off<src.size();) {
    size_t count=std::min<size_t>(chunk,src.size()-off);
    while(count) {
      const auto n=block.append(src.data()+off,count,1);
      off+=n;count-=n;
      if(block.frames==kBlockFrames) flush();
    }
  }
  if(block.frames) flush();
  return result;
}
int main() {
  std::vector<int16_t> src(16000);
  for(size_t i=0;i<src.size();++i) src[i]=(i<8000 ? 0 : (i%2 ? -16000 : 16000));
  std::vector<float> baseline;
  assert(run(src,16000,baseline)==src);
  assert(baseline.size()==100 && baseline[0]==0 && baseline[99]>0);
  for(unsigned n : {1,17,159,160,161,511,4093}) {
    std::vector<float> actual;
    assert(run(src,n,actual)==src && actual==baseline);
  }
  src.resize(1003);
  for(unsigned n : {1,333,1003}) {
    std::vector<float> actual;
    assert(run(src,n,actual)==src && actual.size()==7);
  }
  PcmBlock stereo;
  int16_t input[]={32767,-32767,-32768,32767,1000,1000};
  assert(stereo.append(input,3,2)==3);
  assert(stereo.pcm[0]==0 && stereo.pcm[2]==0 && stereo.pcm[4]==1000);
  auto voiced=process_audio(input,6,1,-45,-15);
  assert(voiced.open>0);
  int16_t quiet[]={1,-1,1,-1};
  assert(process_audio(quiet,4,1,-45,-15).open==0);
  int16_t clip[]={32767,-32768};
  assert(process_audio(clip,2,1,-45,-15).open==1);
  assert(process_audio(nullptr,0,1,-45,-15).open==0);
  const float opened=smooth(0,1,35),closed=smooth(1,0,70);
  assert(opened>.63f && opened<.64f && closed>.36f && closed<.37f);
}
''')

    def test_real_dma_callback_timing_underrun_and_generation_reset(self):
        speaker = (FW / 'speaker.cpp').read_text()
        start = speaker.index('using OutputBlock =')
        end = speaker.index('static void speaker_set_clk', start)
        callback = speaker[start:end]
        self.compile_run(r'''
#include "audio_lipsync.h"
#include <atomic>
#include <cassert>
#include <cstring>
#include <deque>
#include <vector>
#define IRAM_ATTR
using BaseType_t=int;
constexpr int pdFALSE=0,pdTRUE=1,kDmaBufLen=160,kDmaBufCount=3;
struct Queue { size_t size; std::deque<std::vector<unsigned char>> data; };
using QueueHandle_t=Queue*;
using i2s_chan_handle_t=void*;
struct i2s_event_data_t { void* dma_buf; size_t size; };
int xQueueReceiveFromISR(Queue* q,void* dst,int*) {
 if(q->data.empty()) return 0;
 memcpy(dst,q->data.front().data(),q->size);q->data.pop_front();return 1;
}
void xQueueOverwriteFromISR(Queue* q,const void* src,int*) {
 q->data.clear();q->data.emplace_back((const unsigned char*)src,(const unsigned char*)src+q->size);
}
void xQueueReset(Queue* q) { q->data.clear(); }
std::atomic<uint32_t> s_timeline_epoch{0},s_timeline_ms{0},s_output_empty_periods{0};
std::atomic<bool> s_stream_active{true};
int64_t esp_timer_get_time() { return 123456; }
''' + callback + r'''
int main() {
 (void)s_tx;
 Queue output{sizeof(OutputBlock),{}},mouth{sizeof(audio_lipsync::Frame),{}};
 s_output_queue=&output;s_mouth_queue=&mouth;
 int16_t buffers[3][320]{};
 auto complete=[&](int n) {
   // Simulate hardware completion, then IDF's auto_clear_before_cb contract.
   memset(buffers[n],0,sizeof(buffers[n]));
   i2s_event_data_t e{buffers[n],sizeof(buffers[n])};
   on_output_sent(nullptr,&e,nullptr);
 };
 auto snapshot=[&] {
   audio_lipsync::Frame f;
   assert(!mouth.data.empty());memcpy(&f,mouth.data.front().data(),sizeof(f));return f;
 };
 OutputBlock block;block.frames=160;
 for(auto& x:block.pcm) x=1234;
 block.lip.active=true;block.lip.open=.75f;block.lip.generation=1;
 block.lip.epoch=7;block.lip.end_ms=10;
 output.data.emplace_back((unsigned char*)&block,(unsigned char*)&block+sizeof(block));
 s_output_pending=1;
 complete(0);assert(snapshot().open==0 && buffers[0][0]==1234 && s_output_pending==1);
 complete(1);complete(2);assert(s_timeline_epoch==0 && snapshot().underrun);
 complete(0);assert(snapshot().open==.75f && snapshot().epoch==7);
 assert(s_timeline_ms==10 && s_output_pending==0 && buffers[0][0]==0);
 complete(1);assert(snapshot().open==0 && snapshot().active);
 // Cancel with audio both in DMA and waiting for DMA. No old output survives.
 output.data.emplace_back((unsigned char*)&block,(unsigned char*)&block+sizeof(block));
 s_output_pending=1;complete(2);assert(buffers[2][0]==1234);
 reset_output();
 assert(output.data.empty() && mouth.data.empty() && s_output_pending==0);
 assert(s_timeline_epoch==0 && s_output_generation==2);
 for(auto& buffer:buffers) for(auto x:buffer) assert(x==0);
 complete(0);assert(snapshot().generation==2 && snapshot().open==0);
}
''')

    def test_rom_animation_clears_on_underrun_stop_and_new_generation(self):
        self.compile_run(r'''
#include "audio_lipsync.h"
#include <cassert>
using namespace audio_lipsync;
int main() {
  AnimationState state;
  Frame voiced;
  voiced.active=true; voiced.generation=1;
  voiced.open=1;
  process_animation(voiced,10,state);
  assert(state.open>.24f && state.open<.25f);
  process_animation(voiced,35,state);
  assert(state.open>.72f && state.open<.73f);
  Frame quiet=voiced; quiet.open=0;
  const float before=state.open;
  process_animation(quiet,70,state);
  assert(state.open>before*.36f && state.open<before*.37f);
  Frame gap=voiced; gap.underrun=true;
  process_animation(gap,1,state);
  assert(state.open==0);
  process_animation(voiced,10,state);
  assert(state.open>.24f && state.open<.25f);
  process_animation(Frame{},0,state); // Stop/cancel closes even at the same tick.
  assert(state.open==0);
  process_animation(voiced,10000,state);
  assert(state.open>.24f && state.open<.25f); // Idle time is not animation time.
  voiced.generation=2; voiced.open=.2f;
  process_animation(voiced,10000,state);
  assert(state.generation==2 && state.open>.049f && state.open<.05f);
  for(unsigned i=0;i<100;++i) process_animation(voiced,10,state);
  assert(state.open>.199f && state.open<=.2f);
}
''')


    def test_usb_diagnostic_decimation_rejects_aliases(self):
        self.compile_run(r'''
#include "usb_trace_filter.h"
#include <cassert>
#include <cmath>
float measure(float hz) {
 usb_trace::Decimator filter;
 int16_t input[320],output[160]; double energy=0; unsigned samples=0;
 for(unsigned block=0;block<50;++block) {
  for(unsigned i=0;i<320;++i) input[i]=static_cast<int16_t>(6000*std::sin(6.283185307f*hz*(block*320+i)/16000));
  filter.process(input,320,output);
  if(block>0) for(auto value:output) {energy+=double(value)*value;++samples;}
 }
 return std::sqrt(energy/samples);
}
int main() {
 const float pass=measure(1000),stop=measure(6000);
 assert(pass>4100 && pass<4350);
 assert(stop<pass*.01f);
}
''')

    def test_opening_tracks_volume_independent_of_waveform(self):
        self.compile_run(r'''
#include "audio_lipsync.h"
#include <cassert>
using namespace audio_lipsync;
int main() {
  int16_t low[160], high[160], alternate[160], silence[160]{};
  for(unsigned i=0;i<160;++i) {
    low[i]=512; high[i]=4096; alternate[i]=(i%2 ? -4096 : 4096);
  }
  const auto quiet=process_audio(silence,160,1,-45,-15);
  const auto soft=process_audio(low,160,1,-45,-15);
  const auto loud=process_audio(high,160,1,-45,-15);
  const auto other=process_audio(alternate,160,1,-45,-15);
  assert(quiet.open==0 && soft.open>0 && loud.open>soft.open && loud.open<1);
  assert(loud.open==other.open && loud.rms==other.rms);
  AnimationState a,b;
  for(unsigned n=0;n<30;++n) {
    process_animation(loud,10,a); process_animation(other,10,b);
    assert(a.open==b.open);
  }
  assert(a.open>soft.open);
  for(unsigned n=0;n<100;++n)process_animation(quiet,10,a);
  assert(a.open==0);
}
''')
