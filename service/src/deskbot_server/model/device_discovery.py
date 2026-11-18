"""Public endpoint configuration advertised to ROMs; no request-header inference."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit


def endpoint(value: str, *, websocket: bool, resource_base: bool = False) -> str:
    if (not isinstance(value, str) or not value or len(value) > 511
            or any(ord(c) <= 32 or ord(c) >= 127 or c in "\\#" for c in value)):
        raise ValueError("device endpoint must be an ASCII URL or root-relative path, at most 511 characters")
    parsed = urlsplit(value)
    if value.startswith("/"):
        if value.startswith("//") or parsed.netloc:
            raise ValueError("device endpoint cannot be a scheme-relative URL")
    else:
        schemes = {"ws", "wss"} if websocket else {"http", "https"}
        if parsed.scheme not in schemes or not value.startswith(parsed.scheme + "://") or not re.fullmatch(r"[A-Za-z0-9.-]{1,63}(?::[0-9]{1,5})?", parsed.netloc):
            raise ValueError("device endpoint has an invalid scheme, host or port")
        # urlsplit itself rejects ports outside its supported range.
        if parsed.port == 0:
            raise ValueError("device endpoint port must be from 1 to 65535")
    if not parsed.path.startswith("/") or parsed.path == "/" or ".." in parsed.path:
        raise ValueError("device endpoint requires a complete path")
    if any(key in {"device_id", "version"} for key, _ in parse_qsl(parsed.query)):
        raise ValueError("device_id and version are supplied by the ROM")
    if resource_base and ("?" in value or len(value.rstrip("/")) + 17 > 511):
        raise ValueError("face_pack_base_url cannot contain a query and must leave room for the version")
    return value


@dataclass(frozen=True)
class DeviceDiscovery:
    realtime_url: str
    camera_url: str
    camera_enabled: bool
    face_pack_base_url: str

    @classmethod
    def from_server(cls, server: dict) -> "DeviceDiscovery":
        config = server.get("device_bootstrap", {})
        if not isinstance(config, dict):
            raise ValueError("server.device_bootstrap must be an object")
        enabled = config.get("camera_enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("server.device_bootstrap.camera_enabled must be boolean")
        ws_path = server.get("ws_path", "/asr_chat")
        if not isinstance(ws_path, str):
            raise ValueError("server.ws_path must be a string")
        return cls(
            endpoint(config.get("realtime_url", "/" + ws_path.lstrip("/")), websocket=True),
            endpoint(config.get("camera_url", "/camera_uplink"), websocket=True),
            enabled,
            endpoint(config.get("face_pack_base_url", "/api/device/face-pack"), websocket=False, resource_base=True).rstrip("/"),
        )
