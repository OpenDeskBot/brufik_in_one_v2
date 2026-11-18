"""Cross-language envelope vectors and strict C++ bounds checks."""

import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

FW = Path(__file__).resolve().parents[1] / "firmware"


class MediaWireTest(unittest.TestCase):
    def test_cpp_matches_python_vectors_and_rejects_bad_headers(self):
        audio = (
            struct.pack("!2sBBIIIHHI", b"DB", 1, 1, 0x12345678, 3, 240, 80, 4, 3)
            + b"abc"
        )
        ack = struct.pack(
            "!2sBBIIIIIHBBI", b"DB", 1, 3, 0x12345678, 3, 240, 80, 4567, 12, 5, 1, 17
        )
        code = r"""
#include "media_wire.h"
#include <cassert>
#include <cstring>
using namespace media_wire;
int main() {
  uint8_t wire[]={AUDIO};
  uint8_t expected_ack[]={ACK};
  Audio a;
  assert(audio_decode(wire,sizeof(wire),a));
  assert(a.epoch==0x12345678 && a.idx==3 && a.pts==240 && a.duration==80 && a.frames==4 && a.bytes==3);
  uint8_t encoded[24]; audio_encode(encoded,1,a);
  assert(std::memcmp(encoded,wire,24)==0);
  for (size_t i=0;i<sizeof(wire);++i) assert(!audio_decode(wire,i,a));
  wire[2]=2; assert(!audio_decode(wire,sizeof(wire),a)); wire[2]=1;
  wire[3]=2; assert(!audio_decode(wire,sizeof(wire),a)); wire[3]=1;
  write32(wire+4,0); assert(!audio_decode(wire,sizeof(wire),a)); write32(wire+4,1);
  write32(wire+8,0); assert(!audio_decode(wire,sizeof(wire),a)); write32(wire+8,1);
  write32(wire+20,kMaxMedia+1); assert(!audio_decode(wire,sizeof(wire),a));
  uint8_t ack[32]; ack_encode(ack,0x12345678,3,240,80,4567,12,5,true,17);
  assert(std::memcmp(ack,expected_ack,32)==0);
  assert(ack_code("pb_progress")==5 && ack_code("pb_credit")==2 && ack_code("bad")==-1);
}
""".replace("AUDIO", ",".join(map(str, audio))).replace("ACK", ",".join(map(str, ack)))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "wire.cpp"
            binary = Path(tmp) / "wire"
            source.write_text(code)
            subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I" + str(FW),
                    str(source),
                    "-o",
                    str(binary),
                ],
                check=True,
            )
            subprocess.run([str(binary)], check=True)
