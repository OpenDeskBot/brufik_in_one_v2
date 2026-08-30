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


def _build_payload(cfg: DoubaoAsrConfig, pcm_bytes: bytes, sample_rate: int) -> tuple[dict, bytes]:
    """构造一句话识别请求体（4 字节大端长度头 + JSON）。"""
    wav_bytes = pcm_to_wav_bytes(pcm_bytes, sample_rate)
    payload = {
        "app": {"appid": cfg.app_id, "token": cfg.access_token, "cluster": cfg.cluster},
        "user": {"uid": cfg.uid},
        "audio": {"format": "wav", "rate": sample_rate, "bits": 16, "channel": 1},
        "request": {"reqid": uuid.uuid4().hex, "workflow": cfg.workflow, "sequence": -1},
        "audio_data": base64.b64encode(wav_bytes).decode("ascii"),
    }
    body = struct.pack(">I", len(json.dumps(payload).encode("utf-8"))) + json.dumps(payload).encode("utf-8")
    return payload, body


def _duration_ms(pcm_bytes: bytes, sample_rate: int) -> int:
    return int(len(pcm_bytes) / 2 / max(1, sample_rate) * 1000)


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

    payload, body = _build_payload(cfg, pcm_bytes, sample_rate)
    t0 = time.monotonic()
    try:
        http_code, resp = await asyncio.to_thread(_post, cfg.url, body, cfg.access_token)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"豆包 ASR 不可达: {exc.reason}") from exc
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if http_code != 200:  # _post 已捕获 HTTPError：非 200 一律按 HTTP 错误抛出
        detail = json.dumps(resp, ensure_ascii=False)[:200] if resp else ""
        raise RuntimeError(f"豆包 ASR HTTP {http_code}: {detail}")

    text = _parse_response(resp)
    logger.info(
        "[ASR/doubao] req=%s audio_ms=%d elapsed_ms=%d text=%r",
        payload["request"]["reqid"],
        _duration_ms(pcm_bytes, sample_rate),
        elapsed_ms,
        text[:40],
    )
    return text


async def transcribe_doubao_detailed(
    pcm_bytes: bytes, sample_rate: int, cfg: DoubaoAsrConfig
) -> dict:
    """测试专用：同 transcribe_doubao 的请求，但捕获 HTTP / 业务错误并返回详情。

    返回 ``{text, http_code, elapsed_ms, business_code, message}``，不抛业务/
    HTTP 异常（调用方据此渲染测试结果）：

    - 网络不可达/超时 → ``http_code=0``
    - HTTP 4xx/5xx → ``http_code`` 为实际状态码，message 尽力取响应体
    - 业务 code != 0 → ``business_code`` 透出，message 为服务端信息
    - 成功 → ``text`` + ``http_code=200`` + ``business_code=0``

    Raises:
        RuntimeError: 配置缺失 / 音频超时上限（与 transcribe_doubao 一致）。
    """
    if not pcm_bytes:
        return {"text": "", "http_code": 200, "elapsed_ms": 0, "business_code": 0, "message": ""}
    duration_s = len(pcm_bytes) / 2 / max(1, sample_rate)
    if duration_s > MAX_AUDIO_SECONDS:
        raise RuntimeError(f"豆包一句话识别超时上限: {duration_s:.1f}s > {MAX_AUDIO_SECONDS:.0f}s")

    payload, body = _build_payload(cfg, pcm_bytes, sample_rate)
    t0 = time.monotonic()
    try:
        http_code, resp = await asyncio.to_thread(_post, cfg.url, body, cfg.access_token)
    except urllib.error.URLError as exc:
        return {
            "text": "",
            "http_code": 0,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "business_code": None,
            "message": f"豆包 ASR 不可达: {exc.reason}",
        }
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    code = resp.get("code", -1) if isinstance(resp, dict) else -1
    if code != 0:
        msg = str(resp.get("message", "")) if isinstance(resp, dict) else ""
        logger.info(
            "[ASR/doubao] req=%s audio_ms=%d elapsed_ms=%d failed http=%s code=%s msg=%r",
            payload["request"]["reqid"],
            _duration_ms(pcm_bytes, sample_rate),
            elapsed_ms,
            http_code,
            code,
            msg[:80],
        )
        return {
            "text": "",
            "http_code": http_code,
            "elapsed_ms": elapsed_ms,
            "business_code": code,
            "message": msg or f"豆包 ASR 失败 code={code}",
        }
    text = _parse_response(resp)
    logger.info(
        "[ASR/doubao] req=%s audio_ms=%d elapsed_ms=%d text=%r",
        payload["request"]["reqid"],
        _duration_ms(pcm_bytes, sample_rate),
        elapsed_ms,
        text[:40],
    )
    return {
        "text": text,
        "http_code": http_code,
        "elapsed_ms": elapsed_ms,
        "business_code": 0,
        "message": "",
    }


def _post(url: str, body: bytes, token: str) -> tuple[int, dict]:
    """POST 一句话识别 → (http_status, payload)。

    HTTPError 也捕获返回（状态码 + 尽力解析的 JSON），网络不可达仍抛 URLError。
    """
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer; {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TRANSCRIBE_TIMEOUT_S) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            payload = {}
        return exc.code, payload


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
