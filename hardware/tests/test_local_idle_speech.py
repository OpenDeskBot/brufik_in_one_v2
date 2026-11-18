"""Native checks for local speech attention and the real PB idle wait branch."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FW = Path(__file__).resolve().parents[1] / "firmware"


@unittest.skipUnless(shutil.which("c++"), "native C++ compiler unavailable")
class LocalIdleSpeechTest(unittest.TestCase):
    def compile_run(self, code):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.cpp"
            source.write_text(code)
            binary = Path(directory) / "test"
            subprocess.run([
                "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I" + str(FW), str(source), "-o", str(binary),
            ], check=True)
            subprocess.run([str(binary)], check=True)

    def test_speech_blocks_idle_and_restarts_inactivity_after_last_voice(self):
        self.compile_run(r'''
#include "local_idle_policy.h"
#include <cassert>
int main() {
  LocalIdlePolicy idle;
  idle.command(1000);
  assert(!idle.due(30000,15000,12000,false)); // No usable detector yet.
  idle.observe_speech(30000,true,false);
  assert(idle.due(30000,15000,12000,false));
  idle.observe_speech(30001,true,true);
  assert(idle.stop_for_attention());
  assert(!idle.due(90000,15000,12000,false)); // Long speech never restarts idle.
  idle.observe_speech(90000,true,true);
  idle.observe_speech(90001,true,false);
  assert(!idle.stop_for_attention());
  assert(!idle.due(104999,15000,12000,false));
  assert(idle.due(105000,15000,12000,false));
  idle.open_response=true;
  assert(!idle.due(105000,15000,12000,false));
  idle.open_response=false;
  assert(!idle.due(105000,15000,12000,true));
  idle.observe_speech(105001,false,true); // Stale speech bit cannot hide failure.
  assert(!idle.user_speaking && idle.stop_for_attention());
  assert(!idle.due(200000,15000,12000,false));
  idle.observe_speech(200000,true,false);
  assert(idle.due(200000,15000,12000,false));

  LocalIdlePolicy wrap;
  wrap.last_command=wrap.last_idle=0xfffffff0u;
  wrap.observe_speech(0xfffffff0u,true,true);
  wrap.observe_speech(5,true,false);
  assert(!wrap.due(14983,15000,12000,false));
  assert(wrap.due(14984,15000,12000,false));
}
''')

    def test_real_runtime_stops_only_local_idle_on_speech_or_vad_failure(self):
        runtime = (FW / "pb_runtime.cpp").read_text()
        start = runtime.index("static void observe_local_speech()")
        end = runtime.index("static bool make_local_idle", start)
        observer = runtime[start:end]
        start = runtime.index("        if (local_idle) {", runtime.index("while (!(speaker_task_done()"))
        end = runtime.index("        if (local_idle && uxQueueMessagesWaiting", start)
        idle_wait = runtime[start:end]
        self.compile_run(r'''
#include "local_idle_policy.h"
#include <cassert>
#include <string>
using String=std::string;
static LocalIdlePolicy s_idle;
static bool ready=true,healthy=true,speech=false;
static unsigned stopped=0;
uint32_t millis(){return 30000;}
bool audio_frontend_ready(){return ready;}
bool audio_frontend_output_healthy(){return healthy;}
bool audio_frontend_speech_active(){return speech;}
#define log_info(...) ((void)0)
void finish_cancel(const String&,uint32_t,int){++stopped;}
''' + observer + r'''
void run(bool local_idle,bool expected_stop) {
  bool preempted=false;
  const unsigned before=stopped;
  for(unsigned polls=0;polls<3;++polls) {
''' + idle_wait + r'''
  }
  assert(preempted==expected_stop);
  assert(stopped-before==unsigned(expected_stop));
}
int main() {
  run(true,false);
  speech=true;run(true,true);
  run(false,false); // Remote speech/music/performance keep their existing policy.
  speech=false;healthy=false;run(true,true);
  healthy=true;ready=false;run(true,true);
  ready=true;speech=false;run(true,false);
  observe_local_speech();
  speech=true;observe_local_speech(); // Voice can arrive after idle selection.
  assert(s_idle.stop_for_attention());
}
''')


if __name__ == "__main__":
    unittest.main()
