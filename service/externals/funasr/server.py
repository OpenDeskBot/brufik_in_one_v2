#!/usr/bin/env python3
"""funasr：FunASR SenseVoice 独立进程服务（完全自包含）。

使用服务目录内的 deskbot_server 运行子集副本（FunAsrAdapter 等，见
deskbot_server/ 各文件头同步标记），独立 venv 与模型副本由 install.sh 创建，
运行期零依赖主服务：
- GET /health        健康检查
- POST /transcribe   按 ASR 外部服务协议 v1（docs/asr_protocol.md）：
                     PCM int16 LE + header X-Sample-Rate，或 WAV 容器自描述
                     → {"text": ..., "elapsed_ms": ...}；错误统一
                     {"error": {"code", "message"}}。

配置完全自治：只读同目录 config.yaml（主服务 asr 段的独立快照），
不读主服务 config.yaml，也不读 env。

运行：.venv/bin/python server.py [--port 9102]（独立 venv，install.sh 创建）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent  # externals/funasr（独立服务根，含本地 deskbot_server 副本）
sys.path.insert(0, str(SERVICE_ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Header, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from deskbot_server.infrastructure.asr.protocol import (  # noqa: E402
    ERR_MODEL_NOT_READY,
    ERR_TRANSCRIBE_FAILED,
    AsrProtocolError,
    error_response,
    ok_response,
    parse_transcribe_request,
)

logger = logging.getLogger("funasr")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="funasr")
_adapter = None
_adapter_lock = asyncio.Lock()


# 独立配置：完全自治，不读主服务 config.yaml，也不读 env（见同目录 config.yaml）
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def _load_config() -> dict:
    """读取本服务独立 config.yaml；缺文件/解析失败时回退空配置（AppSettings 内置默认值）。"""
    import yaml

    if not CONFIG_PATH.is_file():
        logger.warning("缺少 %s，用默认 ASR 配置", CONFIG_PATH)
        return {}
    try:
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            logger.warning("%s 顶层须为 mapping，用默认 ASR 配置", CONFIG_PATH)
            return {}
        return raw
    except Exception:
        logger.exception("%s 解析失败，用默认 ASR 配置", CONFIG_PATH)
        return {}


@app.on_event("startup")
async def _load_model() -> None:
    global _adapter
    from deskbot_server.infrastructure.asr.funasr import FunAsrAdapter
    from deskbot_server.model.settings import AppSettings

    cfg = _load_config()
    settings = AppSettings.from_config({"asr": cfg})
    _adapter = FunAsrAdapter(settings)
    logger.info("funasr 模型就绪 model_dir=%s", _adapter._model_dir)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "funasr", "model_ready": _adapter is not None})


@app.post("/transcribe")
async def transcribe(
    request: Request,
    x_sample_rate: str | None = Header(None),  # 用 str 让协议层校验非法值（不落 FastAPI 422）
) -> JSONResponse:
    global _adapter
    if _adapter is None:
        return JSONResponse(
            error_response(ERR_MODEL_NOT_READY, "model not ready"), status_code=503
        )
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    try:
        pcm, sample_rate = parse_transcribe_request(body, content_type, x_sample_rate)
    except AsrProtocolError as exc:  # 请求不符合协议：统一标准错误结构
        return JSONResponse(error_response(exc.code, exc.message), status_code=exc.http_status)

    t0 = time.monotonic()
    async with _adapter_lock:  # 与进程内 asr_infer_slot 等价的串行化
        try:
            text = await _adapter.transcribe(pcm, sample_rate)
        except Exception as exc:
            logger.exception("transcribe failed")
            return JSONResponse(error_response(ERR_TRANSCRIBE_FAILED, str(exc)), status_code=500)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return JSONResponse(ok_response(text, elapsed_ms=elapsed_ms))


def main() -> None:
    parser = argparse.ArgumentParser(description="funasr: FunASR SenseVoice 独立进程服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9102)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
