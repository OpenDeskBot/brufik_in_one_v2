"""Decode real service bootstrap data with the same C++ parser used by ROM."""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "hardware/firmware"
JSON_INCLUDE = ROOT / "hardware/.pio/libdeps/seeed_xiao_esp32s3/ArduinoJson/src"
sys.path.insert(0, str(ROOT / "service/src"))
from deskbot_server.service.device_bootstrap_service import DeviceBootstrapService  # noqa: E402


@unittest.skipUnless(
    shutil.which("c++") and JSON_INCLUDE.exists(),
    "host compiler/ArduinoJson unavailable",
)
class BootstrapConfigTest(unittest.TestCase):
    def test_service_response_optional_channels_and_rejected_config_disable_all_media(
        self,
    ):
        package = json.loads((FW / "generated/face_package.json").read_text())
        with patch(
            "deskbot_server.service.device_bootstrap_service.load_face_asset_library",
            return_value=package["library"],
        ):
            body = dict(
                code=0,
                message="ok",
                data=DeviceBootstrapService().bootstrap("rom-test"),
            )
        fixtures = [("normal", body, "ready", True, True)]

        def case(name, mutate, result, compatible=True, camera=False):
            doc = copy.deepcopy(body)
            mutate(doc)
            fixtures.append((name, doc, result, compatible, camera))

        case("absolute_media", lambda d: d["data"]["realtime"].update(url="wss://voice.example:9443/custom/audio?region=cn"), "ready", camera=True)
        case("absolute_camera", lambda d: d["data"]["camera"].update(url="wss://camera.example:9444/live/upload"), "ready", camera=True)
        case("absolute_resource", lambda d: d["data"]["face_pack"].update(url="https://cdn.example:9445/assets/face.bin"), "ready", camera=True)
        case("old_schema", lambda d: d["data"].update(schema=1), "invalid_configuration", False)
        case("no_camera", lambda d: d["data"].pop("camera"), "ready")
        case(
            "camera_disabled",
            lambda d: d["data"].update(camera={"enabled": False}),
            "ready",
        )
        case(
            "no_manifest",
            lambda d: d["data"].pop("face_pack"),
            "invalid_manifest",
            camera=True,
        )
        case(
            "unknown_protocol",
            lambda d: d["data"]["realtime"].update(protocol="unknown"),
            "invalid_configuration",
            False,
        )
        case(
            "wrong_device",
            lambda d: d["data"].update(device_id="someone-else"),
            "invalid_configuration",
            False,
        )
        case(
            "boolean_schema",
            lambda d: d["data"].update(schema=True),
            "invalid_configuration",
            False,
        )
        case(
            "boolean_status",
            lambda d: d.update(code=False),
            "invalid_configuration",
            False,
        )
        case(
            "bad_camera_type",
            lambda d: d["data"].update(camera=[]),
            "invalid_configuration",
            False,
        )
        case(
            "bad_camera_flag",
            lambda d: d["data"]["camera"].update(enabled="false"),
            "invalid_configuration",
            False,
        )
        case(
            "bad_camera_path",
            lambda d: d["data"]["camera"].update(url="//other-host/camera"),
            "invalid_configuration",
            False,
        )
        for index, path in enumerate(
            (
                "/bad\r\npath",
                "/../audio",
                "/bad\0suffix",
            )
        ):
            case(
                f"bad_stream_path_{index}",
                lambda d, p=path: d["data"]["realtime"].update(url=p),
                "invalid_configuration",
                False,
            )
        for index, mutation in enumerate(
            (
                {"size": 0},
                {"size": 256 * 1024 + 1},
                {"size": True},
                {"size": "100"},
                {"sha256": "g" * 64},
                {"version": "x" * 16},
                {"url": "/bad\tpath"},
                {"format": "unknown"},
            )
        ):
            case(
                f"bad_manifest_{index}",
                lambda d, m=mutation: d["data"]["face_pack"].update(m),
                "invalid_manifest",
                camera=True,
            )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            checks = []
            for index, (name, doc, result, compatible, camera) in enumerate(fixtures):
                path = directory / f"{index}.json"
                path.write_text(json.dumps(doc, ensure_ascii=False))
                checks.append(
                    f'check("{path}", Result::{result}, {str(compatible).lower()}, {str(camera).lower()}); // {name}'
                )
            source = directory / "test.cpp"
            source.write_text(
                r"""
#include "bootstrap_config.h"
#include <cassert>
#include <fstream>
#include <iterator>
#include <string>
using namespace bootstrap_config;
void check(const char* path,Result expected,bool compatible,bool camera) {
  std::ifstream file(path);std::string bytes{std::istreambuf_iterator<char>(file),{}};
  JsonDocument doc;assert(!deserializeJson(doc,bytes));
  Settings settings;
  // Reuse deliberately dirty state: a failed next session cannot retain the
  // previous session's channel flags or endpoints.
  settings.compatible=true;settings.camera_enabled=true;
  auto actual=decode(doc.as<JsonVariantConst>(),"rom-test","http://bootstrap.example:9000/deep/discover",settings);
  assert(actual==expected && settings.compatible==compatible);
  assert((settings.compatible && settings.camera_enabled)==camera);
  if (actual==Result::ready) {
    server_endpoint::Url base;
    assert(server_endpoint::bootstrap("http://bootstrap.example:9000/deep/discover",base));
    std::string expected;
    assert(server_endpoint::resolve(base,doc["data"]["realtime"]["url"],true,expected));
    assert(!strcmp(settings.stream_endpoint,expected.c_str()));
    if (camera) {
      assert(server_endpoint::resolve(base,doc["data"]["camera"]["url"],true,expected));
      assert(!strcmp(settings.camera_endpoint,expected.c_str()));
    }
    assert(settings.package.size>0 && strlen(settings.package.sha256)==64);
    assert(strlen(settings.package.version)==16);
    assert(server_endpoint::resolve(base,doc["data"]["face_pack"]["url"],false,expected));
    assert(!strcmp(settings.package.endpoint,expected.c_str()));
  }
}
int main(){
"""
                + "\n".join(checks)
                + "\n}\n"
            )
            binary = directory / "test"
            subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I" + str(JSON_INCLUDE),
                    "-I" + str(FW),
                    str(source),
                    "-o",
                    str(binary),
                ],
                check=True,
            )
            subprocess.run([str(binary)], check=True)
