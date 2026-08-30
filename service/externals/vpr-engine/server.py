#!/usr/bin/env python3
"""vpr-engine：WeSpeaker ResNet34 声纹识别独立进程服务（speaker embedding）。

独立 venv（install.sh 幂等安装 wespeaker + 下载 CN-Celeb ResNet34 模型），
配置完全自治：只读同目录 config.yaml（model_dir/device/min_audio_seconds 等），
不读主服务 config.yaml，也不读 env。

端点（错误统一 {"error": {"code", "message"}}）：
- GET  /health     健康检查（model_ready 供页面区分慢加载）
- POST /voiceprint 契约 vpr：JSON {"audio_base64": str, "sample_rate": int?}
                   audio_base64 为 WAV 容器（自描述，剥头取 int16 mono）或原始
                   PCM int16 LE（配合 sample_rate，默认 16000）
                   → {"embedding": [256 floats], "dim": 256, "elapsed_ms": int}
- POST /compare    声纹比对：JSON {"audio_base64_a", "audio_base64_b",
                   "threshold": float?}（两个音频格式同上）
                   → {"similarity": float, "match": bool?, "elapsed_ms": int}
                   similarity 为两 embedding 余弦相似度；传入 threshold 才返回
                   match（>= threshold 判同人）。

运行：.venv/bin/python server.py [--host 127.0.0.1] [--port 9104]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import logging
import time
import wave
from pathlib import Path

import numpy as np  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

logger = logging.getLogger("vpr-engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="wespeaker-resnet34")

# 错误码（对齐 asr_protocol 风格：统一 {"error": {"code", "message"}}）
ERR_MODEL_NOT_READY = "MODEL_NOT_READY"
ERR_INVALID_AUDIO = "INVALID_AUDIO"
ERR_AUDIO_TOO_SHORT = "AUDIO_TOO_SHORT"
ERR_EXTRACT_FAILED = "EXTRACT_FAILED"

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
DEFAULT_MODEL_DIR = Path(__file__).resolve().parent / "models" / "wespeaker-cnceleb-resnet34"

_embed = None
_request_lock = asyncio.Lock()  # torch 推理串行化（与 funasr/insightface-engine 同策略）

# 运行时配置（startup 加载）
_min_seconds = 0.0
_max_seconds = 0.0


def _load_config() -> dict:
    """读取本服务独立 config.yaml；缺文件/解析失败时回退空配置（用代码内置默认值）。"""
    import yaml

    if not CONFIG_PATH.is_file():
        logger.warning("缺少 %s，用默认参数", CONFIG_PATH)
        return {}
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            logger.warning("%s 顶层须为 mapping，用默认参数", CONFIG_PATH)
            return {}
        return raw
    except Exception:
        logger.exception("%s 解析失败，用默认参数", CONFIG_PATH)
        return {}


@app.on_event("startup")
async def _load_model() -> None:
    global _embed, _min_seconds, _max_seconds
    cfg = _load_config()

    model_dir = Path(__file__).resolve().parent / str(cfg.get("model_dir") or "") if cfg.get("model_dir") else DEFAULT_MODEL_DIR
    _min_seconds = float(cfg.get("min_audio_seconds") or 0.3)
    _max_seconds = float(cfg.get("max_audio_seconds") or 30)

    try:
        # torchaudio 2.x 兼容（wespeaker master 的 s3prl 旧生态依赖）：
        # set_audio_backend 与 sox_effects 模块已移除——s3prl 仅 import 引用不调用，
        # 占位即可（非 fbank 前端才实例化 s3prl，ResNet34 走 fbank 不受影响）
        import sys
        import types

        import torchaudio

        if not hasattr(torchaudio, "set_audio_backend"):
            torchaudio.set_audio_backend = lambda *a, **k: None
        if not hasattr(torchaudio, "sox_effects"):
            _dummy = types.ModuleType("torchaudio.sox_effects")
            _dummy.apply_effects_tensor = lambda *a, **k: None
            sys.modules["torchaudio.sox_effects"] = _dummy
            torchaudio.sox_effects = _dummy

        import wespeaker.cli.speaker as _spk

        _spk.load_silero_vad = lambda: None  # 不用 VAD（apply_vad=False），避免其联网下载模型
        from wespeaker.cli.speaker import Speaker

        _embed = Speaker(model_dir=str(model_dir))
        logger.info("vpr-engine 模型就绪 model_dir=%s", model_dir)
    except Exception:
        logger.exception("WeSpeaker 模型加载失败（/voiceprint 返回 503，health 报 model_ready=false）")


def _error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _decode_audio(data: dict) -> tuple[np.ndarray, int]:
    """解析 audio_base64 → (int16 mono PCM, sample_rate)。

    WAV 容器自描述（剥头取 int16 mono，立体声取左声道，8bit 升 16bit）；
    否则视为原始 PCM int16 LE，采样率取 body.sample_rate（默认 16000）。
    异常抛 ValueError（message 面向调用方）。
    """
    raw_b64 = str(data.get("audio_base64") or "").strip()
    if not raw_b64:
        raise ValueError("audio_base64 不能为空")
    try:
        raw = base64.b64decode(raw_b64)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"audio_base64 非法: {exc}") from exc
    if not raw:
        raise ValueError("audio_base64 解码为空")
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("音频过大（>16MB）")

    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        with wave.open(io.BytesIO(raw), "rb") as w:
            channels, sample_rate = w.getnchannels(), w.getframerate()
            sampwidth = w.getsampwidth()
            frames = w.readframes(w.getnframes())
        if not frames:
            raise ValueError("WAV 音频内容为空")
        if sampwidth == 1:
            frames = bytes(b for byte in frames for b in (byte, 0))  # 8bit → 16bit
        elif sampwidth != 2:
            raise ValueError(f"不支持的 WAV 位深: {sampwidth * 8}bit（仅 8/16bit）")
        pcm = np.frombuffer(frames, dtype=np.int16)
        if channels > 1:
            pcm = pcm[0::channels]  # 立体声/多声道 → 左声道
        return pcm, sample_rate

    # 原始 PCM int16 LE
    try:
        sample_rate = int(data.get("sample_rate") or 16000)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"sample_rate 非法: {exc}") from exc
    if sample_rate <= 0:
        raise ValueError(f"sample_rate 非法: {sample_rate}")
    pcm = np.frombuffer(raw, dtype=np.int16)
    return pcm, sample_rate


async def _extract_embedding(pcm: np.ndarray, sample_rate: int) -> list[float]:
    """提取 256 维 speaker embedding（串行化；Speaker 内部重采样到 16k + fbank）。

    pcm 为 int16 mono（WAV 剥头后）；转 float torch.Tensor 直接喂
    extract_embedding_from_pcm（apply_vad=False，不裁剪静音）。
    """
    if len(pcm) == 0:
        raise ValueError("音频内容为空")
    secs = len(pcm) / sample_rate
    if secs < _min_seconds:
        raise ValueError(f"音频过短（{secs:.1f}s，至少 {_min_seconds:.1f}s）")
    if _max_seconds > 0 and secs > _max_seconds:
        pcm = pcm[: int(_max_seconds * sample_rate)]  # 过长截断（防异常输入拖垮推理）
    import torch

    tensor = torch.from_numpy(np.ascontiguousarray(pcm)).unsqueeze(0)  # [1, N] int16
    async with _request_lock:
        try:
            emb = await asyncio.to_thread(_embed.extract_embedding_from_pcm, tensor, sample_rate)
        except Exception as exc:
            raise RuntimeError(f"embedding 提取失败: {exc}") from exc
    if emb is None or not len(emb):
        raise RuntimeError("embedding 提取失败：返回空向量")
    return [float(x) for x in emb.detach().cpu().numpy().ravel()]


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {"ok": True, "service": "wespeaker-resnet34", "model_ready": _embed is not None}
    )


@app.post("/voiceprint")
async def voiceprint(request: Request) -> JSONResponse:
    """契约 vpr：JSON {audio_base64, sample_rate?} → {"embedding": [...], "dim": N}。"""
    if _embed is None:
        return _error_response(ERR_MODEL_NOT_READY, "model not ready", 503)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return _error_response(ERR_INVALID_AUDIO, "body 须为 JSON object", 400)
    except Exception:
        return _error_response(ERR_INVALID_AUDIO, "invalid json", 400)
    try:
        pcm, sample_rate = _decode_audio(data)
    except ValueError as exc:
        return _error_response(ERR_INVALID_AUDIO, str(exc), 400)
    t0 = time.monotonic()
    try:
        emb = await _extract_embedding(pcm, sample_rate)
    except ValueError as exc:  # 过短等输入问题
        return _error_response(ERR_AUDIO_TOO_SHORT, str(exc), 422)
    except RuntimeError as exc:
        logger.exception("voiceprint failed")
        return _error_response(ERR_EXTRACT_FAILED, str(exc), 500)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return JSONResponse({"embedding": emb, "dim": len(emb), "elapsed_ms": elapsed_ms})


@app.post("/compare")
async def compare(request: Request) -> JSONResponse:
    """声纹比对：两个音频 embedding 的余弦相似度（供后续声纹识别玩法）。

    JSON {"audio_base64_a": str, "audio_base64_b": str, "threshold": float?}
    → {"similarity": float, "match": bool?（传 threshold 时）, "elapsed_ms": int}
    """
    if _embed is None:
        return _error_response(ERR_MODEL_NOT_READY, "model not ready", 503)
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return _error_response(ERR_INVALID_AUDIO, "body 须为 JSON object", 400)
    except Exception:
        return _error_response(ERR_INVALID_AUDIO, "invalid json", 400)
    threshold: float | None = None
    if data.get("threshold") is not None:
        try:
            threshold = float(data["threshold"])
        except (TypeError, ValueError):
            return _error_response(ERR_INVALID_AUDIO, "threshold 非法", 400)
    t0 = time.monotonic()
    try:
        emb_a = await _extract_embedding(*_decode_audio({"audio_base64": data.get("audio_base64_a")}))
        emb_b = await _extract_embedding(*_decode_audio({"audio_base64": data.get("audio_base64_b")}))
    except ValueError as exc:
        return _error_response(ERR_AUDIO_TOO_SHORT, str(exc), 422)
    except RuntimeError as exc:
        logger.exception("compare failed")
        return _error_response(ERR_EXTRACT_FAILED, str(exc), 500)
    va, vb = np.asarray(emb_a), np.asarray(emb_b)
    norm = float(np.linalg.norm(va) * np.linalg.norm(vb))
    similarity = float(np.dot(va, vb) / norm) if norm > 0 else 0.0
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    payload: dict = {"similarity": similarity, "elapsed_ms": elapsed_ms}
    if threshold is not None:
        payload["match"] = similarity >= threshold
    return JSONResponse(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="vpr-engine: WeSpeaker ResNet34 声纹识别独立进程服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9104)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
