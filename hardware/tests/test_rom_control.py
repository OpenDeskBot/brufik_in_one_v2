"""Native checks for the exact volume and bitmap bounds used by ROM tools."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("c++"), reason="native compiler unavailable")
def test_rom_volume_limits_and_exact_image_size(tmp_path):
    source = tmp_path / "rom_control.cpp"
    source.write_text('''
#include "rom_control.h"
#include <cassert>
int main(){
 int out=0;using rom_control::VolumeAction;
 assert(rom_control::volume_target(95,10,VolumeAction::Adjust,out)&&out==100);
 assert(rom_control::volume_target(5,-10,VolumeAction::Adjust,out)&&out==0);
 assert(rom_control::volume_target(40,55,VolumeAction::Set,out)&&out==55);
 assert(rom_control::volume_target(40,0,VolumeAction::Get,out)&&out==40);
 assert(!rom_control::volume_target(40,-1,VolumeAction::Set,out));
 assert(!rom_control::volume_target(40,101,VolumeAction::Adjust,out));
 assert(rom_control::image_size(284,240,136320,284,240));
 assert(!rom_control::image_size(284,240,136319,284,240));
 assert(!rom_control::image_size(240,284,136320,284,240));
}
''')
    executable = tmp_path / "check"
    firmware = Path(__file__).resolve().parents[1] / "firmware"
    subprocess.run(["c++", "-std=c++17", "-I", str(firmware), str(source), "-o", str(executable)], check=True)
    subprocess.run([str(executable)], check=True)
