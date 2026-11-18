"""Compile the exact URL rules used by ROM provisioning and discovery."""

import subprocess
import tempfile
import unittest
from pathlib import Path

FW = Path(__file__).resolve().parents[1] / "firmware"


class ServerEndpointTest(unittest.TestCase):
    def test_exact_bootstrap_paths_independent_targets_and_tls(self):
        source = r'''
#include "server_endpoint.h"
#include <cassert>
using namespace server_endpoint;
int main() {
  Url base, target;
  assert(bootstrap("https://discovery.example:9443/tenant/robot/discover", base));
  assert(base.path=="/tenant/robot/discover" && base.port==9443 && base.secure());
  assert(with_device("https://discovery.example:9443/tenant/robot/discover","rom-1")==
      "https://discovery.example:9443/tenant/robot/discover?device_id=rom-1");
  for (auto bad : {"http://host", "https://host/", "ws://host/start", "wss://host/start",
      "http://host:0/start", "http://host:abc/start", "http://host:65536/start", "http://host:12x/start",
      "http://user@host/start", "http://host/path#fragment", "http://host/path?x=1",
      "http://host/../path", "http://host//other", "http://host/path\r\nHeader:x"}) {
    assert(!bootstrap(bad,target));
  }
  std::string result;
  assert(resolve(base,"/public/media?zone=cn",true,result));
  assert(result=="wss://discovery.example:9443/public/media?zone=cn");
  assert(with_device(result,"rom-1")=="wss://discovery.example:9443/public/media?zone=cn&device_id=rom-1");
  assert(resolve(base,"wss://voice.example:8443/v2/audio/",true,result));
  assert(result=="wss://voice.example:8443/v2/audio/"); // Preserve trailing slash.
  assert(parse(result,target) && target.host=="voice.example" && target.port==8443);
  assert(resolve(base,"wss://camera.example:8444/v2/upload",true,result));
  assert(parse(result,target) && target.host=="camera.example" && target.port==8444);
  assert(resolve(base,"https://cdn.example/assets/version.json",false,result));
  assert(!resolve(base,"ws://voice.example/audio",true,result));
  assert(!resolve(base,"http://cdn.example/face",false,result));
  assert(!resolve(base,"//other.example/audio",true,result));
  assert(!resolve(base,"relative/audio",true,result));
  assert(!resolve(base,"https://host/audio",true,result));
  assert(!resolve(base,"wss://host/audio?device_id=someone",true,result));
  assert(bootstrap("http://local.example/config",base));
  assert(resolve(base,"/audio",true,result) && result=="ws://local.example/audio");
  assert(resolve(base,"wss://voice.example/audio",true,result));
  assert(resolve(base,"https://cdn.example/face",false,result));
  assert(!bootstrap(("http://host/"+std::string(512,'x')).c_str(),target));
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "test.cpp").write_text(source)
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{FW}", str(path / "test.cpp"), "-o", str(path / "test")], check=True)
            subprocess.run([str(path / "test")], check=True)
