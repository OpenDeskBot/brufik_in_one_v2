# === externals/funasr 本地副本（funasr 独立化，勿直接删改此段）===
# 来源: src/deskbot_server/utils/audio.py
# 同步: 主服务改动后按 docs/external_services.md「funasr」节同步本副本并更新日期
# synced: 2026-08-30
"""音频工具：PCM→WAV 转换与临时文件。"""

from __future__ import annotations

import io
import os
import tempfile
import wave


def pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_bytes)
    return buf.getvalue()


def save_temp_wav(pcm_bytes: bytes, sample_rate: int) -> str:
    wav_bytes = pcm_to_wav_bytes(pcm_bytes, sample_rate)
    fd, path = tempfile.mkstemp(prefix="bot_", suffix=".wav")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(wav_bytes)
    return path
