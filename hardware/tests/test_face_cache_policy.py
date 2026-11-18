"""Inject cuts/failures into the same durable update ordering used by ROM."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FW = Path(__file__).resolve().parents[1] / "firmware"


@unittest.skipUnless(shutil.which("c++"), "native C++ compiler unavailable")
class FaceCachePolicyTest(unittest.TestCase):
    def test_every_commit_boundary_preserves_a_verified_same_server_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "test.cpp"
            source.write_text(r"""
#include "face_cache_policy.h"
#include <cassert>
#include <string>
#include <functional>
struct Reset {};
struct Store {
  // Stored hashes use identity strings here; actual SHA-256/format validation
  // is exercised by HTTP and downloaded-loader tests, not this storage model.
  std::string files[2]={"old-program","previous-program"};
  std::string hashes[2]={"old-program","previous-program"};
  std::string origins[2]={"server-A","server-A"};
  std::string wanted="server-A",temporary,loaded;
  unsigned selected=0,step=0,fail=0,cut=0;
  bool corrupt_write=false,partial_replace=false;
  bool perform(const std::function<void()>& change) {
    ++step;if(step==fail)return false;
    change();if(step==cut)throw Reset{};return true;
  }
  bool load_if_valid(unsigned n) {
    if(origins[n]!=wanted || files[n].empty() || files[n]!=hashes[n])return false;
    loaded=files[n];return true;
  }
  bool write_temporary(){return perform([&]{temporary=corrupt_write?"broken":"new-program";});}
  bool verify_temporary(){return perform([]{}) && temporary=="new-program";}
  bool replace_slot(unsigned n){return perform([&]{files[n].clear();if(partial_replace)throw Reset{};files[n]=temporary;temporary.clear();});}
  bool write_metadata(unsigned n){return perform([&]{hashes[n]="new-program";origins[n]=wanted;});}
  bool select_slot(unsigned n){return perform([&]{selected=n;});}
};
void recover(Store& s) {
  const int slot=face_cache_policy::recover(s,s.selected);
  assert(slot>=0);
  assert(s.loaded=="old-program" || s.loaded=="new-program");
  assert(s.files[slot]==s.hashes[slot] && s.origins[slot]==s.wanted);
}
int main() {
  Store normal;assert(face_cache_policy::commit(normal,0));recover(normal);assert(normal.loaded=="new-program");
  for(unsigned boundary=1;boundary<=5;++boundary) {
    Store fail;fail.fail=boundary;assert(!face_cache_policy::commit(fail,0));recover(fail);assert(fail.loaded=="old-program");
    Store cut;cut.cut=boundary;try{face_cache_policy::commit(cut,0);assert(false);}catch(Reset&){}recover(cut);
  }
  Store corrupt;corrupt.corrupt_write=true;assert(!face_cache_policy::commit(corrupt,0));recover(corrupt);assert(corrupt.loaded=="old-program");
  Store partial;partial.partial_replace=true;try{face_cache_policy::commit(partial,0);assert(false);}catch(Reset&){}recover(partial);assert(partial.loaded=="old-program");
  Store missing;missing.files[0]="truncated";assert(face_cache_policy::recover(missing,0)==1);assert(missing.loaded=="previous-program");
  missing.wanted="server-B";assert(face_cache_policy::recover(missing,0)==-1);
  // After fallback, protect the recovered slot, even if the persisted pointer was bad.
  missing.wanted="server-A";assert(face_cache_policy::commit(missing,1));assert(missing.selected==0);recover(missing);assert(missing.loaded=="new-program");
  Store invalid_pointer;assert(face_cache_policy::recover(invalid_pointer,255)==1);
}
""")
            binary = Path(tmp) / "test"
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
