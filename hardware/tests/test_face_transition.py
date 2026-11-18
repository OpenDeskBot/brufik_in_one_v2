"""Exercise the exact interpolation math used by the display, without display hardware."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class TransitionTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'native C++ compiler unavailable')
    def test_switch_cancel_shape_change_and_incremental_mouth(self):
        code = r'''
#include "face_transition.h"
#include <cassert>
struct Prim {
 int shape=1; uint16_t color=65535;
 int x=0,y=0,w=10,h=10,r=0,x1=0,y1=0,x2=0,y2=0;
 int text_size=1; char text[8]{};
};
struct Layer { unsigned count=0; Prim prims[16]; };
int main() {
 Layer a,b,shown,next;
 a.count=b.count=1; a.prims[0].x=0; b.prims[0].x=100;
 face_transition::layer(&a,b,0.f,shown); assert(shown.prims[0].x==0);
 face_transition::layer(&a,b,.4f,shown); assert(shown.prims[0].x==40);
 // An interruption captures shown, not the old target at x=100.
 next.count=1; next.prims[0].x=80; Layer snapshot=shown;
 face_transition::layer(&snapshot,next,0.f,shown); assert(shown.prims[0].x==40);
 face_transition::layer(&snapshot,next,.5f,shown); assert(shown.prims[0].x==60);
 face_transition::layer(&snapshot,next,1.f,shown); assert(shown.prims[0].x==80);
 // Missing eye/decoration layers in a mouth chunk retain the last visible face.
 Layer omitted;
 face_transition::layer(&snapshot,omitted,.9f,shown); assert(shown.prims[0].x==40);
 // Different primitive types disappear before the new geometry appears.
 b.prims[0].shape=2;
 face_transition::layer(&a,b,.25f,shown); assert(shown.prims[0].shape==1 && shown.prims[0].color!=65535);
 face_transition::layer(&a,b,.5f,shown); assert(shown.prims[0].color==0);
 face_transition::layer(&a,b,1.f,shown); assert(shown.prims[0].shape==2 && shown.prims[0].color==65535);
 // Extra outgoing slots fade out and are removed at the exact endpoint.
 a.count=2;
 face_transition::layer(&a,b,.5f,shown); assert(shown.count==2 && shown.prims[1].color!=65535);
 face_transition::layer(&a,b,1.f,shown); assert(shown.count==1);
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'test.cpp').write_text(code)
            fw=Path(__file__).resolve().parents[1]/'firmware'
            subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror','-I'+str(fw),str(root/'test.cpp'),'-o',str(root/'test')],check=True)
            subprocess.run([str(root/'test')],check=True)
