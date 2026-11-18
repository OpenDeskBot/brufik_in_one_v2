"""Run with: python3 -m unittest discover -s hardware/tests -p test_face_assets.py."""
import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('face_assets', ROOT / 'hardware/tools/face_assets.py')
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)


class FaceAssetsTest(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(assets.SOURCE.read_text())

    def test_repeatable_and_content_versioned(self):
        first = assets.compile_editor_document(self.doc)
        self.assertEqual(first, assets.compile_editor_document(json.loads(json.dumps(self.doc, sort_keys=True))))
        self.doc['emotions'][0]['title'] = '只改显示名称'
        self.assertEqual(first['version'], assets.compile_editor_document(self.doc)['version'])
        self.doc['emotions'][0]['frames'][0]['ms'] += 1
        self.assertNotEqual(first['version'], assets.compile_editor_document(self.doc)['version'])

    def test_compact_frames_preserve_every_primitive(self):
        bundle = assets.compile_editor_document(self.doc)
        frame_index = 0
        for scene, meta in zip(self.doc['emotions'], bundle['assets']):
            self.assertEqual(meta['duration_ms'], sum(f['ms'] for f in scene['frames']))
            for frame in scene['frames']:
                offset, count, ms = bundle['frames'][frame_index]
                expected = []
                for layer in assets.LAYERS:
                    ps = frame['elements'].get(layer, [])
                    if layer in ('nose', 'extra') and not ps:
                        ps = [{'shape': 'circle', 'x': 0, 'y': 0, 'r': 0, 'c': 0}]
                    for source_primitive in ps:
                        p = dict(source_primitive)
                        if p['shape'] in ('hline', 'vline'):
                            p['h' if p['shape'] == 'hline' else 'w'] = 1
                            p['shape'] = 'rect'
                        coords = [p.get(k, p.get({'w':'rw', 'h':'rh', 'r':'radius'}.get(k,''), 0)) for k in assets.FIELDS]
                        expected.append((layer, p['shape'], p.get('c',65535), *coords, p.get('size',1), p.get('text','')))
                self.assertEqual(bundle['primitives'][offset:offset+count], expected)
                self.assertEqual(ms, frame['ms'])
                frame_index += 1
        self.assertLess(len(bundle['primitives']), sum(f[1] for f in bundle['frames']))

    def test_phonemes_exported_and_versioned(self):
        first=assets.compile_editor_document(self.doc)
        self.assertEqual(len(first['phonemes']),44)
        self.assertEqual(first['schema'],2)
        for scene,meta in zip(self.doc['phonemes'],first['phonemes']):
            self.assertEqual(meta['id'],scene['name'])
            self.assertEqual(meta['aliases'],scene['alias'])
            for i,frame in enumerate(scene['frames']):
                offset,count,ms=first['frames'][meta['frame_offset']+i]
                ps=first['primitives'][offset:offset+count]
                expected=frame['elements']['mouth']
                actual=[p for p in ps if p[0]=='mouth']
                self.assertEqual(len(actual),len(expected))
                for p,q in zip(actual,expected):
                    self.assertEqual((p[1],p[3],p[4],p[5],p[6]),(q['shape'],q['x'],q['y'],q['w'],q['h']))
                self.assertEqual(ms,frame['ms'])
        self.doc['phonemes'][0]['frames'][0]['elements']['mouth'][0]['w']+=1
        self.assertNotEqual(first['version'],assets.compile_editor_document(self.doc)['version'])

    def test_reject_invalid_assets(self):
        mutations = [
            lambda d: d['emotions'][0]['alias'].append(d['emotions'][1]['name']),
            lambda d: d['emotions'][0]['frames'][0].update(ms=True),
            lambda d: d['emotions'][0]['frames'][0]['elements'].update(eye_l=[]),
            lambda d: d['emotions'][0]['frames'][0]['elements']['eye_l'][0].update(shape='triangle'),
            lambda d: d['emotions'][0]['frames'][0]['elements']['eye_l'][0].update(x=32768),
            lambda d: d['emotions'][0]['frames'][0]['elements']['eye_l'][0].update(text='你'*43),
            lambda d: d['emotions'][0]['frames'][0]['elements']['extra'].extend([{}]*17),
        ]
        for mutate in mutations:
            doc = copy.deepcopy(self.doc)
            mutate(doc)
            with self.assertRaises(ValueError):
                assets.compile_editor_document(doc)

    def test_generated_files_are_checked_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets.generate(output=tmp)
            path = Path(tmp) / 'face_assets.inc'
            stamp = path.stat().st_mtime_ns
            assets.generate(output=tmp)
            self.assertEqual(stamp, path.stat().st_mtime_ns)
            assets.generate(output=tmp, check=True)
            path.write_text('stale')
            with self.assertRaises(ValueError):
                assets.generate(output=tmp, check=True)

    @unittest.skipUnless(shutil.which('c++'), 'native C++ compiler unavailable')
    def test_cpp_loader_ids_aliases_and_allocation_failure_cleanup(self):
        bundle = assets.compile_editor_document(self.doc)
        checks = []
        for a in bundle['assets']:
            for key in [a['id']] + a['aliases']:
                checks.append(f'check("{key}", {a["frame_count"]}, {a["duration_ms"]});')
        for a in bundle['phonemes']:
            for key in [a['id']] + a['aliases']:
                checks.append(f'check("{key}", {a["frame_count"]}, 123, true);')
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'Arduino.h').write_text('#pragma once\n#include <cstdint>\n#include <cstddef>\n')
            (p / 'ArduinoJson.h').write_text('#pragma once\nclass JsonDocument;\n')
            (p / 'esp_heap_caps.h').write_text('''#pragma once
#include <cstddef>
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
void* heap_caps_calloc(size_t, size_t, int);
''')
            (p / 'test.cpp').write_text(r'''
#include "face_assets.h"
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <set>
static std::set<void*> live;
static int fail_at = -1, calls = 0;
void* heap_caps_calloc(size_t n, size_t size, int) {
  if (calls++ == fail_at) return nullptr;
  void* p = calloc(n, size); assert(p); live.insert(p); return p;
}
static void release(void* p) { if(p) { assert(live.erase(p)==1); free(p); } }
void pb_anim_frames_free(pb_anim_frame* f, size_t n) {
  for(size_t i=0; i<n; ++i) release(f[i].elements);
  release(f);
}
static bool load(const char* id, bool mouth, pb_anim_frame*& f, size_t& n, uint32_t& ms, const char*& error) {
  if (!mouth) return face_assets_load(id,f,n,ms,error);
  ms=0;
  bool ok=face_assets_load_phoneme(id,123,f,n,error);
  if(ok) ms=123;
  return ok;
}
static void check(const char* id, size_t expected, uint32_t ms, bool mouth=false) {
  pb_anim_frame* f; size_t n; uint32_t duration; const char* error;
  fail_at=-1; calls=0;
  assert(load(id,mouth,f,n,duration,error));
  assert(n==expected && duration==ms && !error);
  uint32_t total=0;
  for(size_t i=0;i<n;++i) { assert(f[i].element_count>0); total+=f[i].ms; }
  assert(total==duration);
  if(mouth) for(size_t i=0;i<n;++i) {
    assert(f[i].ms>0 && strcmp(f[i].phoneme,id)==0);
    for(size_t j=0;j<f[i].element_count;++j) assert(f[i].elements[j].layer==pb_anim_element_layer::mouth);
  }
  pb_anim_frames_free(f,n); assert(live.empty());
  for(size_t i=0;i<=expected;++i) {
    fail_at=i; calls=0;
    assert(!load(id,mouth,f,n,duration,error));
    assert(!f && n==0 && duration==0 && error && live.empty());
  }
}
int main() {
''' + '\n'.join(checks) + f'\nassert(strcmp(face_assets_version(),"{bundle["version"]}")==0);' + r'''
  pb_anim_frame* f; size_t n; uint32_t ms; const char* error;
  assert(!face_assets_load("missing_id",f,n,ms,error));
  assert(!f && n==0 && ms==0 && live.empty());
  assert(!face_assets_load(nullptr,f,n,ms,error));
  assert(!face_assets_load_phoneme("unknown",100,f,n,error));
  assert(!face_assets_load_phoneme("ai",1,f,n,error));
  assert(!face_assets_load_phoneme("a",30001,f,n,error));
  assert(!f && n==0 && live.empty());
  fail_at=-1; calls=0;
  assert(face_assets_load_frame("happy",0,80,f,n,error));
  assert(n==1 && f[0].ms==80 && f[0].element_count>0);
  pb_anim_frames_free(f,n); assert(live.empty());
  assert(!face_assets_load_frame("happy",63,80,f,n,error));
  assert(!f && n==0 && live.empty());
  assert(!face_assets_load_frame("happy",0,0,f,n,error));
  fail_at=1; calls=0;
  assert(!face_assets_load_frame("happy",0,80,f,n,error));
  assert(!f && n==0 && live.empty());
}
''')
            subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror', '-I'+tmp,
                            '-I'+str(ROOT/'hardware/firmware'),str(p/'test.cpp'),
                            str(ROOT/'hardware/firmware/face_assets.cpp'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)


if __name__ == '__main__':
    unittest.main()
