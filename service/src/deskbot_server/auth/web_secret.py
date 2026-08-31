"""Web 会话 / debug token 共用密钥解析。

优先级：
1. 环境变量 ``DESKBOT_WEB_SECRET_KEY``（生产推荐，跨实例一致）
2. 运行时文件 ``data/global/web_secret_key`` —— 首次启动自动生成
   （``secrets.token_hex(32)``，0600），后续启动复用，避免每次重启登录态失效。

⚠️ 不再回落硬编码默认值：否则伪造 session / debug token 成为可能。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

_SECRET_FILE_NAME = "web_secret_key"

_cached: str | None = None


def _secret_file() -> Path:
    from deskbot_server.utils.device_data import global_config_dir

    return global_config_dir() / _SECRET_FILE_NAME


def resolve_web_secret_key() -> str:
    """返回当前进程生效的 secret；env 优先，否则读取/生成运行时文件。"""
    global _cached
    env = (os.environ.get("DESKBOT_WEB_SECRET_KEY") or "").strip()
    if env:
        _cached = env
        return env
    if _cached:
        return _cached

    path = _secret_file()
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            _cached = value
            return value

    value = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _cached = value
    except OSError:
        # 无法落盘（只读目录等）：进程内可用，重启后 session 失效
        _cached = value
    return value
