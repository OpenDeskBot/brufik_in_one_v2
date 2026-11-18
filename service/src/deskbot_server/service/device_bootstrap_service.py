"""HTTP control plane: public, non-secret device discovery and versioned assets."""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from threading import RLock

from deskbot_server.config import get_config
from deskbot_server.dao.face_asset_catalog import load_face_asset_library
from deskbot_server.model.device_discovery import DeviceDiscovery
from deskbot_server.model.face_package import encode_package

_lock = RLock()
_packages: OrderedDict[str, tuple[dict, bytes]] = OrderedDict()
_source_version = ""
_current = None


class DeviceBootstrapService:
    def package(self, version: str | None = None) -> tuple[dict, bytes]:
        global _source_version, _current
        bundle = load_face_asset_library()
        with _lock:
            if bundle["version"] != _source_version:
                payload = encode_package(bundle)
                metadata = dict(
                    version=bundle["version"],
                    format="deskbot-face-v1",
                    sha256=hashlib.sha256(payload).hexdigest(),
                    size=len(payload),
                    url=f"/api/device/face-pack/{bundle['version']}",
                )
                _packages[bundle["version"]] = (metadata, payload)
                while len(_packages) > 4:
                    _packages.popitem(last=False)
                _current, _source_version = metadata, bundle["version"]
            if version is not None:
                if not re.fullmatch(r"[0-9a-f]{16}", version) or version not in _packages:
                    raise KeyError(version)
                metadata, payload = _packages[version]
                return dict(metadata), payload
            metadata, payload = _packages[_current["version"]]
            return dict(metadata), payload

    def bootstrap(self, device_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", device_id):
            raise ValueError("invalid device_id")
        endpoints = DeviceDiscovery.from_server(get_config().get("server", {}))
        metadata, _ = self.package()
        metadata["url"] = f"{endpoints.face_pack_base_url}/{metadata['version']}"
        return dict(
            schema=2,
            device_id=device_id,
            face_pack=metadata,
            realtime=dict(protocol="deskbot.pb.v2", url=endpoints.realtime_url),
            camera=dict(enabled=endpoints.camera_enabled, url=endpoints.camera_url),
            audio=dict(input_codec="opus", output_codec="opus", sample_rate=16000, channels=1),
            capabilities=["local_face_pack_v1", "pcm_lipsync_v1", "local_idle_v1"],
        )
