"""Compile the real downloaded-package loader with real ArduinoJson on the host."""

import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "hardware/firmware"
JSON_INCLUDE = ROOT / "hardware/.pio/libdeps/seeed_xiao_esp32s3/ArduinoJson/src"
spec = importlib.util.spec_from_file_location(
    "assets_package", ROOT / "hardware/tools/face_assets.py"
)
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


@unittest.skipUnless(
    shutil.which("c++") and JSON_INCLUDE.exists(),
    "host compiler/ArduinoJson unavailable",
)
class FacePackageTest(unittest.TestCase):
    def test_real_loader_validation_aliases_mapping_and_failed_update_preserves_active(
        self,
    ):
        bundle = compiler.compile_editor_document(json.loads(compiler.SOURCE.read_text()))
        package = json.loads(json.dumps(dict(format="deskbot-face-v1", library=bundle)))
        bad = []
        for mutate in (
            lambda d: d.update(format="unsupported"),
            lambda d: d["library"]["frames"][0].__setitem__(0, 999999),
            lambda d: d["library"]["primitives"][0].__setitem__(0, None),
            lambda d: d["library"]["primitives"][0].__setitem__(3, True),
            lambda d: d["library"]["primitives"][0].__setitem__(13, "x" * 129),
            lambda d: d["library"]["assets"][0]["aliases"].append(None),
            lambda d: d["library"]["assets"].append(
                copy.deepcopy(d["library"]["assets"][0])
            ),
            lambda d: d["library"]["assets"][0]["aliases"].append(
                d["library"]["assets"][0]["id"]
            ),
            lambda d: d["library"]["assets"][0].update(frame_count=65),
            lambda d: d["library"]["phonemes"][0].update(duration_ms=1),
            lambda d: d["library"]["lipsync"].update(open_phoneme="missing"),
            lambda d: d["library"]["lipsync"].update(open_phoneme=None),
            lambda d: d["library"]["lipsync"].update(closed_phoneme=42),
            lambda d: d["library"]["lipsync"].update(classifier="unsupported"),
            lambda d: d["library"].update(lipsync=[]),
            lambda d: d["library"]["idle"]["clips"][0].update(anim_id="missing"),
            lambda d: d["library"]["idle"]["clips"][1]["servo"][0].update(x=1000),
            lambda d: d["library"]["idle"].update(after_ms=0),
        ):
            doc = copy.deepcopy(package)
            mutate(doc)
            bad.append(json.dumps(doc, ensure_ascii=False))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "Arduino.h").write_text(
                "#pragma once\n#include <cstdint>\n#include <cstddef>\n"
            )
            (p / "esp_heap_caps.h").write_text("""#pragma once
#include <cstdlib>
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
extern int allocation_fail,allocation_calls,live_allocations;
inline void* heap_caps_malloc(size_t n,int){return malloc(n);}
inline void* heap_caps_calloc(size_t n,size_t s,int){
  if(allocation_calls++==allocation_fail)return nullptr;
  void* p=calloc(n,s);if(p)++live_allocations;return p;
}
inline void* heap_caps_realloc(void* p,size_t n,int){return realloc(p,n);}
inline void heap_caps_free(void* p){free(p);}
""")
            (p / "valid.json").write_text(json.dumps(package, ensure_ascii=False))
            for i, doc in enumerate(bad):
                (p / f"bad{i}.json").write_text(doc)
            updated = copy.deepcopy(package)
            updated["library"]["version"] = "0123456789abcdef"
            updated["library"]["lipsync"]["open_phoneme"] = "i"
            # Old cached packages still load without usable voice calibration.
            # Only the closed/open endpoints are consumed by the ROM now.
            updated["library"]["lipsync"].update(
                classifier="mfcc-vowel-v1", profiles=None,
                feature="obsolete", vowels={"A": "missing"},
            )
            (p / "updated.json").write_text(json.dumps(updated, ensure_ascii=False))
            (p / "test.cpp").write_text(r"""
#include "face_package.h"
#include "face_package_data.h"
#include "local_idle_policy.h"
#include <cassert>
#include <fstream>
#include <iterator>
#include <string>
int allocation_fail=-1,allocation_calls=0,live_allocations=0;
void release(void* p){if(p){--live_allocations;free(p);}}
void pb_anim_frames_free(pb_anim_frame* f,size_t n){for(size_t i=0;i<n;++i)release(f[i].elements);release(f);}
std::string read(const char* p){std::ifstream f(p);return {std::istreambuf_iterator<char>(f),{}};}
int main(int argc,char** argv){
 auto good=read(argv[1]);
 assert(face_package_install((const uint8_t*)good.data(),good.size()));
 std::string version=face_package_version();
 assert(face_package_mouth(false)==std::string("sil"));
 JsonDocument root;assert(!deserializeJson(root,good));auto lib=root["library"];
 for(const char* kind:{"assets","phonemes"})for(JsonObject entry:lib[kind].as<JsonArray>()){
   for(unsigned alias=0;alias<=entry["aliases"].size();++alias){
     const char* id=alias?entry["aliases"][alias-1].as<const char*>():entry["id"].as<const char*>();
     const bool mouth=!strcmp(kind,"phonemes");pb_anim_frame* f=nullptr;size_t count=0;uint32_t duration=0;const char* error=nullptr;
     assert(face_package_load(id,mouth,333,-1,f,count,duration,error));
     assert(count==entry["frame_count"].as<unsigned>() && !error);
     assert(duration==(mouth?333:entry["duration_ms"].as<unsigned>()));
     unsigned sum=0;for(unsigned i=0;i<count;++i){sum+=f[i].ms;assert(f[i].element_count>0);
       if(mouth)for(unsigned j=0;j<f[i].element_count;++j)assert(f[i].elements[j].layer==pb_anim_element_layer::mouth);}
     assert(sum==duration);pb_anim_frames_free(f,count);
   }
 }
 for(int i=3;i<argc;++i){auto bad=read(argv[i]);assert(!face_package_install((const uint8_t*)bad.data(),bad.size()));assert(version==face_package_version());}
 assert(!face_package_install((const uint8_t*)good.data(),good.size(),"ffffffffffffffff"));
 assert(!face_package_install((const uint8_t*)good.data(),good.size()-8));
 assert(!face_package_install((const uint8_t*)good.data(),face_package_data::kMaxBytes+1));
 assert(version==face_package_version());
 auto updated=read(argv[2]);assert(face_package_validate((const uint8_t*)updated.data(),updated.size(),"0123456789abcdef"));
 assert(version==face_package_version()); // validation must not activate before durable commit
 assert(face_package_install((const uint8_t*)updated.data(),updated.size()));assert(!strcmp(face_package_mouth(true),"i"));
 pb_anim_frame* f=nullptr;size_t n=0;uint32_t ms=0;const char* error=nullptr;
 assert(face_package_load("happy",false,87,0,f,n,ms,error));assert(n==1 && ms==87);pb_anim_frames_free(f,n);
 assert(!face_package_load("happy",false,87,999,f,n,ms,error));assert(!f && !n && !ms);
 unsigned frames=face_package_data::find(lib["assets"],"happy")["frame_count"];
 for(unsigned fail=0;fail<=frames;++fail){
   allocation_fail=fail;allocation_calls=0;
   assert(!face_package_load("happy",false,0,-1,f,n,ms,error));
   assert(!f && !n && !ms && error && live_allocations==0);
 }
 allocation_fail=-1;
 LocalIdlePolicy idle;idle.command(1000);idle.observe_speech(1000,true,false);
 assert(!idle.due(15999,15000,1000,false));assert(idle.due(16000,15000,1000,false));
 idle.open_response=true;assert(!idle.due(90000,15000,1000,false));idle.open_response=false;
 assert(!idle.due(90000,15000,1000,true));idle.last_idle=90000;
 assert(!idle.due(90100,15000,1000,false));idle.command(90100);assert(!idle.due(100000,15000,1000,false));
 idle.last_command=0xfffffff0;idle.last_idle=0xfffffff0;assert(idle.due(15000,15000,1000,false));
}
""")
            subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-DARDUINOJSON_ENABLE_ARDUINO_STRING=0",
                    "-DARDUINOJSON_ENABLE_ARDUINO_STREAM=0",
                    "-I" + tmp,
                    "-I" + str(JSON_INCLUDE),
                    "-I" + str(FW),
                    str(p / "test.cpp"),
                    str(FW / "face_package.cpp"),
                    "-o",
                    str(p / "test"),
                ],
                check=True,
            )
            subprocess.run(
                [
                    str(p / "test"),
                    str(p / "valid.json"),
                    str(p / "updated.json"),
                    *[str(p / f"bad{i}.json") for i in range(len(bad))],
                ],
                check=True,
            )
