#!/usr/bin/env python3
"""insightface-engine：本地人脸检测与识别独立进程服务（完全自包含）。

代码：deskbot_server/ 运行子集副本（随仓库提交，各文件头带同步标记），import 解析到本服务目录，
运行期零依赖主服务源码；venv/模型均自备（.venv + models/，install.sh 幂等）。
- CameraFaceDetector（MediaPipe 检测）
- face_identity.deduplicate_overlapping_faces / attach_descriptors_to_faces（去重 + embedding/几何特征）
- face_identity.descriptor_from_jpeg_base64（单脸 embedding；失败回退几何特征）

配置完全自治：只读同目录 config.yaml（主服务 camera_face 段的独立快照），
不读主服务 config.yaml，也不读 env。

端点：
- GET  /health      健康检查
- POST /detect      契约 fr：body=JPEG bytes → {"faces": [...]}
- POST /embedding   单张人脸 JPEG+landmarks → {"embedding": [...], "descriptor_kind": ...}

运行：.venv/bin/python server.py [--host 127.0.0.1] [--port 9103] [--workers N]
（独立 venv；workers 缺省读同目录 config.yaml 的 workers 字段，默认 min(4, CPU 核数)，
每个 worker 独立进程加载一份模型，多核服务器可并行推理）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

SERVICE_ROOT = Path(__file__).resolve().parent  # externals/insightface-engine/
# 【独立化】import deskbot_server.* 解析到本服务运行子集副本（deskbot_server/），
# 运行期零依赖主服务 src（主服务改动须按 docs/external_services.md 同步副本）
sys.path.insert(0, str(SERVICE_ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

logger = logging.getLogger("insightface-engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="insightface-engine")

_detector = None
_embedding_ready = False
_request_lock = asyncio.Lock()  # MediaPipe detector 非线程安全，推理串行化

# 独立配置：完全自治，不读主服务 config.yaml，也不读 env（见同目录 config.yaml）
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def _load_config() -> dict[str, Any]:
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
async def _load_models() -> None:
    global _detector, _embedding_ready
    from deskbot_server.service.application.face_detector import CameraFaceDetector
    from deskbot_server.vision.camera_face_tune import set_face_embedding_enabled
    from deskbot_server.vision.undistort import try_build_undistorter

    cfg = _load_config()

    # 对齐主服务 face_embedding_enabled 开关（独立进程无调试页热更新，取文件配置）
    fe_raw = cfg.get("face_embedding_enabled", True)
    set_face_embedding_enabled(str(fe_raw).strip().lower() not in ("0", "false", "no", "off"))

    _detector = CameraFaceDetector(
        num_faces=int(cfg.get("num_faces") or 5),
        undistorter=try_build_undistorter(cfg),
        min_face_detection_confidence=float(cfg.get("min_face_detection_confidence") or 0.5),
        min_face_presence_confidence=float(cfg.get("min_face_presence_confidence") or 0.5),
        frame_width=int(cfg.get("frame_width") or 320),
        frame_height=int(cfg.get("frame_height") or 240),
    )
    logger.info(
        "insightface-engine MediaPipe 就绪 num_faces=%d frame=%dx%d",
        _detector.num_faces,
        _detector.frame_width,
        _detector.frame_height,
    )

    # 预载 InsightFace embedding；失败仅回退几何特征，不阻塞服务
    try:
        from deskbot_server.vision.face_embedding import get_face_embedding_engine

        eng = get_face_embedding_engine()
        _embedding_ready = eng is not None
        logger.info("insightface-engine embedding %s", "就绪" if _embedding_ready else "不可用（回退几何特征）")
    except Exception:
        logger.exception("InsightFace 预载失败（回退几何特征）")


def _serialize_faces(faces: list[dict[str, Any]], opts_frame: tuple[int, int]) -> list[dict[str, Any]]:
    """按 camera_face_service._mp_recognize 的字段序列化（仅参考原实现，不改原代码）。"""
    out: list[dict[str, Any]] = []
    for face in faces or []:
        if not isinstance(face, dict):
            continue
        emb = face.get("embedding") or face.get("face_descriptor")
        if emb is not None and not isinstance(emb, list):
            emb = list(emb)
        row: dict[str, Any] = {
            "landmarks": face.get("landmarks") or [],
            "embedding": emb,
            "image_w": int(face.get("image_w") or opts_frame[0]),
            "image_h": int(face.get("image_h") or opts_frame[1]),
        }
        if emb is not None:
            row["face_descriptor"] = emb
        if face.get("descriptor_kind"):
            row["descriptor_kind"] = face["descriptor_kind"]
        if face.get("facial_transform") is not None:
            row["facial_transform"] = face["facial_transform"]
        out.append(row)
    return out


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {"ok": True, "service": "insightface-engine", "model_ready": _detector is not None, "embedding_ready": _embedding_ready}
    )


@app.post("/detect")
async def detect(request: Request) -> JSONResponse:
    """契约 fr：body=JPEG bytes → {"faces": [{landmarks, embedding, ...}, ...]}。"""
    if _detector is None:
        return JSONResponse({"error": "model not ready"}, status_code=503)
    jpeg = await request.body()
    if not jpeg:
        return JSONResponse({"error": "empty jpeg"}, status_code=400)
    async with _request_lock:
        try:
            from deskbot_server.vision.face_identity import (
                attach_descriptors_to_faces,
                deduplicate_overlapping_faces,
            )

            faces = _detector.detect_faces(jpeg)
            faces = deduplicate_overlapping_faces(faces)
            attach_descriptors_to_faces(faces, bgr_image=_detector.last_bgr)
            out = _serialize_faces(faces, (_detector.frame_width, _detector.frame_height))
        except Exception:
            logger.exception("detect failed")
            return JSONResponse({"error": "detect failed"}, status_code=500)
    return JSONResponse({"faces": out})


@app.post("/embedding")
async def embedding(request: Request) -> JSONResponse:
    """单张人脸 JPEG+landmarks → 描述符（embedding 优先，失败回退几何，同 attach_descriptor 语义）。

    输入 JSON：{"jpeg_base64": str, "landmarks": [{"name", "x", "y"}, ...]}
    输出：{"embedding": [...], "descriptor_kind": "embedding" | "geometry"}（不可用时 embedding=null）
    """
    if _detector is None:
        return JSONResponse({"error": "model not ready"}, status_code=503)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    jpeg_b64 = str(payload.get("jpeg_base64") or "").strip()
    landmarks = payload.get("landmarks") if isinstance(payload.get("landmarks"), list) else []
    if not jpeg_b64:
        return JSONResponse({"error": "jpeg_base64 required"}, status_code=400)
    async with _request_lock:
        try:
            from deskbot_server.vision.face_embedding import is_embedding_vector
            from deskbot_server.vision.face_identity import compute_face_descriptor, descriptor_from_jpeg_base64

            desc = descriptor_from_jpeg_base64(jpeg_b64, landmarks)
            kind = "embedding"
            if desc is None:
                desc = compute_face_descriptor(landmarks)
                kind = "geometry"
        except Exception:
            logger.exception("embedding failed")
            return JSONResponse({"error": "embedding failed"}, status_code=500)
    if desc is None:
        return JSONResponse({"embedding": None, "descriptor_kind": None})
    if not is_embedding_vector(desc):
        kind = "geometry"
    return JSONResponse({"embedding": desc, "descriptor_kind": kind})


def _resolve_workers(args_workers: int) -> int:
    """worker 数：--workers 参数 > config.yaml workers 字段 > 默认 min(4, CPU 核数)。

    每个 worker 是独立进程，各自加载一份 MediaPipe + InsightFace 模型（内存 ~N 倍），
    多核服务器按核数配置即可并行推理。
    """
    cpu = os.cpu_count() or 2
    default_n = max(1, min(4, cpu))
    if args_workers and args_workers > 0:
        n = int(args_workers)
    else:
        n = int(_load_config().get("workers") or default_n)
    return max(1, min(n, cpu))


def main() -> None:
    parser = argparse.ArgumentParser(description="insightface-engine: 本地人脸检测与识别独立进程服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9103)
    parser.add_argument("--workers", type=int, default=0, help="并行推理进程数；0=读 config.yaml workers（默认 min(4, CPU 核数)）")
    args = parser.parse_args()
    n = _resolve_workers(args.workers)
    if n <= 1:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return
    # uvicorn 多 worker 要求 app 为 import string：子进程各自 import server.py，
    # 各自跑 startup（加载自己的 detector/embedding），请求按 socket 分发到各 worker 并行处理
    logger.info("insightface-engine workers=%d（每 worker 独立加载模型）", n)
    uvicorn.run("server:app", host=args.host, port=args.port, workers=n, log_level="info")


if __name__ == "__main__":
    main()
