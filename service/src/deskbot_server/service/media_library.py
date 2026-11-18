"""Normalize uploaded audio/images once, then stream device-ready assets."""

from __future__ import annotations

import io
import subprocess
import tempfile
import uuid
import wave

import numpy as np
from PIL import Image, ImageOps

from deskbot_server.dao import media_asset_store as store
from deskbot_server.pb.display import FACE_LCD_HEIGHT, FACE_LCD_WIDTH

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def upload(device_id: str, kind: str, title: str, data: bytes) -> dict:
    if kind not in {"music", "image"} or not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("请选择图片或音乐文件，单个文件不超过 20 MB")
    if len(store.list_assets(device_id)) >= 100:
        raise ValueError("每台设备最多保存 100 个媒体资源，请先删除不需要的资料")
    aid = uuid.uuid4().hex
    item = {"id": aid, "kind": kind, "title": str(title or "未命名媒体").strip()[:100]}
    paths = []
    try:
        if kind == "image":
            with Image.open(io.BytesIO(data)) as source:
                if source.width * source.height > 16_000_000:
                    raise ValueError("图片像素过大")
                picture = ImageOps.exif_transpose(source).convert("RGBA")
                # Enlarge small uploads too, keeping the entire picture visible.
                picture = ImageOps.contain(
                    picture, (FACE_LCD_WIDTH, FACE_LCD_HEIGHT), Image.Resampling.LANCZOS,
                )
                canvas = Image.new("RGB", (FACE_LCD_WIDTH, FACE_LCD_HEIGHT), "black")
                canvas.paste(picture, ((FACE_LCD_WIDTH-picture.width)//2, (FACE_LCD_HEIGHT-picture.height)//2), picture)
            preview = store.asset_path(device_id, aid, "png")
            paths.append(preview)
            canvas.save(preview)
            values = np.asarray(canvas, dtype=np.uint16)
            packed = ((values[:, :, 0] >> 3) << 11) | ((values[:, :, 1] >> 2) << 5) | (values[:, :, 2] >> 3)
            raw = store.asset_path(device_id, aid, "rgb565")
            paths.append(raw)
            raw.write_bytes(packed.astype("<u2").tobytes())
            item.update(width=FACE_LCD_WIDTH, height=FACE_LCD_HEIGHT)
        else:
            with tempfile.NamedTemporaryFile(suffix=".media") as source:
                source.write(data)
                source.flush()
                try:
                    result = subprocess.run([
                        "ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
                        "-format_whitelist", "mp3,wav,flac,ogg,mov,matroska,webm", "-i", source.name,
                        "-t", "180", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
                    ], capture_output=True, timeout=30, check=False)
                except FileNotFoundError as exc:
                    raise ValueError("服务端需要安装 ffmpeg 才能接收音乐") from exc
            if result.returncode or len(result.stdout) < 640:
                raise ValueError("无法读取此音乐文件，请上传 WAV、MP3、FLAC、OGG 或 M4A")
            wav_path = store.asset_path(device_id, aid, "wav")
            paths.append(wav_path)
            with wave.open(str(wav_path), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(result.stdout)
            item["duration_ms"] = len(result.stdout) // 32
        store.save(device_id, item)
        return item
    except (OSError, subprocess.TimeoutExpired, Image.DecompressionBombError) as exc:
        raise ValueError("媒体文件处理失败") from exc
    finally:
        if not store.asset_path(device_id, aid, "json").exists():
            for path in paths:
                path.unlink(missing_ok=True)
