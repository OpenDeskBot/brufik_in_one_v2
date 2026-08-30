"""豆包（火山引擎）ASR：一句话识别 v1。

配置走环境变量（与豆包 TTS 同模式，不落 config.yaml）：
- DOUBAO_ASR_APP_ID       应用 ID（控制台创建应用并开通一句话识别）
- DOUBAO_ASR_ACCESS_TOKEN 应用令牌（Authorization: Bearer; {token}）
- DOUBAO_ASR_CLUSTER      Cluster ID（控制台开通服务后显示）
- DOUBAO_ASR_URL          端点，默认 https://openspeech.bytedance.com/api/v1/asr
- DOUBAO_ASR_UID          用户标识（默认 deskbot）
- DOUBAO_ASR_WORKFLOW     识别流程（默认标准版；留空用内置默认）

协议：POST，请求体 = 4 字节大端长度头 + JSON（app/user/audio/request + audio_data
base64 wav）。响应 {"code": 0, "message": "Success", "result": [...]}。
参考 docs/asr_protocol.md 的云服务接入约定（本模块直连云 API，不走外部协议 HTTP 层）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import time
import urllib.error
import urllib.request
import uuid

from deskbot_server.utils.audio import pcm_to_wav_bytes
from deskbot_server.utils.env import load_dotenv

logger = logging.getLogger("deskbot-server")

DEFAULT_URL = "https://openspeech.bytedance.com/api/v1/asr"
DEFAULT_UID = "deskbot"
# 标准版识别流程（含标点）；极速版/大模型版可用 DOUBAO_ASR_WORKFLOW 覆盖
DEFAULT_WORKFLOW = "audio_in,resample,partition,vad,fe,decode,itn,nlu_punctuate"
MAX_AUDIO_SECONDS = 60.0  # 一句话识别时长上限
TRANSCRIBE_TIMEOUT_S = 30.0


class DoubaoAsrConfig:
    def __init__(
        self,
        *,
        app_id: str,
        access_token: str,
        cluster: str,
        url: str = DEFAULT_URL,
        uid: str = DEFAULT_UID,
        workflow: str = "",
    ) -> None:
        self.app_id = app_id
        self.access_token = access_token
        self.cluster = cluster
        self.url = url
        self.uid = uid
        self.workflow = workflow or DEFAULT_WORKFLOW

    def validate(self) -> None:
        missing = [k for k, v in (("app_id", self.app_id), ("access_token", self.access_token), ("cluster", self.cluster)) if not v]
        if missing:
            raise RuntimeError(
                f"豆包 ASR 缺少配置: {', '.join('DOUBAO_ASR_' + m.upper() for m in missing)}"
                "（火山引擎控制台 → 创建应用并开通一句话识别后填写）"
            )


def load_doubao_asr_config() -> DoubaoAsrConfig:
    """从 env 加载豆包 ASR 配置（load_dotenv 每次调用，.env 可热改）。"""
    load_dotenv()
    return DoubaoAsrConfig(
        app_id=(os.environ.get("DOUBAO_ASR_APP_ID") or "").strip(),
        access_token=(os.environ.get("DOUBAO_ASR_ACCESS_TOKEN") or "").strip(),
        cluster=(os.environ.get("DOUBAO_ASR_CLUSTER") or "").strip(),
        url=(os.environ.get("DOUBAO_ASR_URL") or DEFAULT_URL).strip(),
        uid=(os.environ.get("DOUBAO_ASR_UID") or DEFAULT_UID).strip(),
        workflow=(os.environ.get("DOUBAO_ASR_WORKFLOW") or "").strip(),
    )


async def transcribe_doubao(pcm_bytes: bytes, sample_rate: int, cfg: DoubaoAsrConfig) -> str:
    """PCM → wav → 火山一句话识别 v1 → 识别文本。

    Raises:
        RuntimeError: 配置缺失 / 网络不可达 / 服务端 code != 0。
    """
    if not pcm_bytes:
        return ""
    duration_s = len(pcm_bytes) / 2 / max(1, sample_rate)
    if duration_s > MAX_AUDIO_SECONDS:
        raise RuntimeError(f"豆包一句话识别超时上限: {duration_s:.1f}s > {MAX_AUDIO_SECONDS:.0f}s")

    wav_bytes = pcm_to_wav_bytes(pcm_bytes, sample_rate)
    payload = {
        "app": {"appid": cfg.app_id, "token": cfg.access_token, "cluster": cfg.cluster},
        "user": {"uid": cfg.uid},
        "audio": {"format": "wav", "rate": sample_rate, "bits": 16, "channel": 1},
        "request": {"reqid": uuid.uuid4().hex, "workflow": cfg.workflow, "sequence": -1},
        "audio_data": base64.b64encode(wav_bytes).decode("ascii"),
    }
    body = struct.pack(">I", len(json.dumps(payload).encode("utf-8"))) + json.dumps(payload).encode("utf-8")

    t0 = time.monotonic()
    try:
        resp = await asyncio.to_thread(_post, cfg.url, body, cfg.access_token)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"豆包 ASR 不可达: {exc.reason}") from exc
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"豆包 ASR HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:200]}") from exc
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    text = _parse_response(resp)
    logger.info("[ASR/doubao] req=%s audio_ms=%d elapsed_ms=%d text=%r", payload["request"]["reqid"], int(duration_s * 1000), elapsed_ms, text[:40])
    return text


def _post(url: str, body: bytes, token: str) -> dict:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer; {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=TRANSCRIBE_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_response(resp: dict) -> str:
    code = resp.get("code", -1)
    if code != 0:
        raise RuntimeError(f"豆包 ASR 失败 code={code} message={resp.get('message', '')}")
    result = resp.get("result")
    # 标准版：result 为数组 [{text, ...}]；部分版本为字符串
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return str(result[0].get("text", "")).strip()
    raise RuntimeError(f"豆包 ASR 响应异常: {resp}")
