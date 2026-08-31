"""豆包（火山引擎）ASR 2.0：Seed-ASR 大模型极速版（一次性识别 Flash）。

配置优先走设备级 ``devices.asr_param``（resolve.py 注入 overrides），
全局兜底走环境变量（与旧版同模式，不落 config.yaml）：
- DOUBAO_ASR_API_KEY     新版控制台「API Key 管理」的 API Key（Authorization: X-Api-Key）
- DOUBAO_ASR_RESOURCE_ID 资源 ID（默认 volc.seedasr.auc）
- DOUBAO_ASR_UID         用户标识（默认 deskbot）
- DOUBAO_ASR_URL         端点（默认 https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash）

协议（Express Flash，一次性识别）：
POST JSON（Content-Type: application/json），请求体
``{"user": {"uid"}, "audio": {"data": <base64 wav>, "format": "wav"},
 "request": {"model_name": "bigmodel", "enable_punc": true, "enable_itn": true}}``；
鉴权头 ``X-Api-Key``（新版控制台 ASR 2.0 不支持 AppId+AccessToken），
另带 ``X-Api-Resource-Id`` / ``X-Api-Request-Id`` / ``X-Api-Sequence: -1``。
**结果状态在响应头** ``X-Api-Status-Code``：20000000 成功（正文 result.text）、
20000003 静音（成功但空文本）；其余失败，错误信息在 ``X-Api-Message`` 响应头。
参考：vahnxu/doubao-asr（Express tier）。v1 一句话识别（appid/token/cluster）已移除。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid

from deskbot_server.utils.audio import pcm_to_wav_bytes
from deskbot_server.utils.env import load_dotenv

logger = logging.getLogger("deskbot-server")

DEFAULT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
DEFAULT_UID = "deskbot"
DEFAULT_RESOURCE_ID = "volc.seedasr.auc"
MAX_AUDIO_SECONDS = 60.0  # 一次性识别时长上限
TRANSCRIBE_TIMEOUT_S = 30.0

# X-Api-Status-Code 响应头取值（业务状态码）
STATUS_OK = "20000000"  # 识别成功
STATUS_SILENCE = "20000003"  # 静音（成功，空文本）
_STATUS_OK_CODES = (STATUS_OK, STATUS_SILENCE)

# 表单字段名（robot-settings 对话框 / asr_test 覆盖字段 / resolve overrides 共用）
DOUBAO_ASR_FIELDS = ("api_key", "resource_id", "uid", "url")


class DoubaoAsrConfig:
    def __init__(
        self,
        *,
        api_key: str = "",
        resource_id: str = DEFAULT_RESOURCE_ID,
        uid: str = DEFAULT_UID,
        url: str = DEFAULT_URL,
    ) -> None:
        self.api_key = api_key
        self.resource_id = resource_id or DEFAULT_RESOURCE_ID
        self.uid = uid or DEFAULT_UID
        self.url = url or DEFAULT_URL

    def validate(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "豆包 ASR 缺少 API Key（设备「配置」或 DOUBAO_ASR_API_KEY）"
                "（火山引擎控制台 → API Key 管理）"
            )


def load_doubao_asr_config() -> DoubaoAsrConfig:
    """从 env 加载豆包 ASR 配置（load_dotenv 每次调用，.env 可热改）。"""
    load_dotenv()
    return DoubaoAsrConfig(
        api_key=(os.environ.get("DOUBAO_ASR_API_KEY") or "").strip(),
        resource_id=(os.environ.get("DOUBAO_ASR_RESOURCE_ID") or "").strip(),
        uid=(os.environ.get("DOUBAO_ASR_UID") or "").strip(),
        url=(os.environ.get("DOUBAO_ASR_URL") or "").strip(),
    )


def merge_doubao_config(base: DoubaoAsrConfig, overrides: dict[str, str]) -> DoubaoAsrConfig:
    """非空 overrides 字段覆盖 base，返回新实例（不修改入参）。"""
    return DoubaoAsrConfig(
        api_key=str((overrides.get("api_key") or "").strip() or base.api_key),
        resource_id=str((overrides.get("resource_id") or "").strip() or base.resource_id),
        uid=str((overrides.get("uid") or "").strip() or base.uid),
        url=str((overrides.get("url") or "").strip() or base.url),
    )


def _build_payload(cfg: DoubaoAsrConfig, pcm_bytes: bytes, sample_rate: int) -> tuple[str, bytes]:
    """构造 Flash 识别请求体（纯 JSON；音频 base64 内联 wav）。"""
    wav_bytes = pcm_to_wav_bytes(pcm_bytes, sample_rate)
    payload = {
        "user": {"uid": cfg.uid},
        "audio": {"data": base64.b64encode(wav_bytes).decode("ascii"), "format": "wav"},
        "request": {
            "model_name": "bigmodel",
            "enable_punc": True,
            "enable_itn": True,
        },
    }
    reqid = uuid.uuid4().hex
    return reqid, json.dumps(payload).encode("utf-8")


def _duration_ms(pcm_bytes: bytes, sample_rate: int) -> int:
    return int(len(pcm_bytes) / 2 / max(1, sample_rate) * 1000)


def _post(url: str, body: bytes, cfg: DoubaoAsrConfig, reqid: str) -> tuple[int, dict, dict]:
    """POST Flash 识别 → (http_status, resp_headers, payload)。

    HTTPError 也捕获返回（状态码 + 尽力解析的 JSON），网络不可达仍抛 URLError。
    """
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": cfg.api_key,
            "X-Api-Resource-Id": cfg.resource_id,
            "X-Api-Request-Id": reqid,
            "X-Api-Sequence": "-1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TRANSCRIBE_TIMEOUT_S) as resp:
            return resp.getcode(), dict(resp.headers), json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            payload = {}
        return exc.code, dict(exc.headers), payload


def _extract_text(resp: dict) -> str:
    """成功响应取识别文本；result 为 dict（{"text", "utterances"}）。"""
    result = resp.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str):
            return text.strip()
    raise RuntimeError(f"豆包 ASR 响应异常: {resp}")


def _status_code(headers: dict) -> str:
    """响应头 X-Api-Status-Code；缺失时按成功处理（HTTP 200 + result.text 亦可）。"""
    return str((headers.get("X-Api-Status-Code") or "").strip())


async def transcribe_doubao(pcm_bytes: bytes, sample_rate: int, cfg: DoubaoAsrConfig) -> str:
    """PCM → wav → Seed-ASR Flash → 识别文本。

    Raises:
        RuntimeError: 配置缺失 / 网络不可达 / HTTP 错误 / 业务 status != 成功。
    """
    if not pcm_bytes:
        return ""
    duration_s = len(pcm_bytes) / 2 / max(1, sample_rate)
    if duration_s > MAX_AUDIO_SECONDS:
        raise RuntimeError(f"豆包 ASR 时长上限: {duration_s:.1f}s > {MAX_AUDIO_SECONDS:.0f}s")

    reqid, body = _build_payload(cfg, pcm_bytes, sample_rate)
    t0 = time.monotonic()
    try:
        http_code, headers, resp = await asyncio.to_thread(_post, cfg.url, body, cfg, reqid)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"豆包 ASR 不可达: {exc.reason}") from exc
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if http_code != 200:
        detail = json.dumps(resp, ensure_ascii=False)[:200] if resp else ""
        raise RuntimeError(f"豆包 ASR HTTP {http_code}: {detail}")

    status = _status_code(headers)
    if status and status not in _STATUS_OK_CODES:
        raise RuntimeError(f"豆包 ASR 失败 status={status} message={headers.get('X-Api-Message', '')}")

    text = "" if status == STATUS_SILENCE else _extract_text(resp)
    logger.info(
        "[ASR/doubao] req=%s audio_ms=%d elapsed_ms=%d status=%s text=%r",
        reqid, _duration_ms(pcm_bytes, sample_rate), elapsed_ms, status or "20000000", text[:40],
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
    - 业务 status != 20000000/20000003 → ``business_code`` 透出（X-Api-Status-Code），
      message 为 ``X-Api-Message``
    - 成功 → ``text`` + ``http_code=200`` + ``business_code=20000000``
    - 静音（20000003）→ 成功语义，text 为空串

    Raises:
        RuntimeError: 配置缺失 / 音频时长超上限（与 transcribe_doubao 一致）。
    """
    if not pcm_bytes:
        return {"text": "", "http_code": 200, "elapsed_ms": 0, "business_code": STATUS_OK, "message": ""}
    duration_s = len(pcm_bytes) / 2 / max(1, sample_rate)
    if duration_s > MAX_AUDIO_SECONDS:
        raise RuntimeError(f"豆包 ASR 时长上限: {duration_s:.1f}s > {MAX_AUDIO_SECONDS:.0f}s")

    reqid, body = _build_payload(cfg, pcm_bytes, sample_rate)
    t0 = time.monotonic()
    try:
        http_code, headers, resp = await asyncio.to_thread(_post, cfg.url, body, cfg, reqid)
    except urllib.error.URLError as exc:
        return {
            "text": "",
            "http_code": 0,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "business_code": None,
            "message": f"豆包 ASR 不可达: {exc.reason}",
        }
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    status = _status_code(headers)
    if http_code != 200:
        detail = json.dumps(resp, ensure_ascii=False)[:200] if resp else ""
        return {
            "text": "",
            "http_code": http_code,
            "elapsed_ms": elapsed_ms,
            "business_code": status or None,
            "message": f"豆包 ASR HTTP {http_code}: {detail}",
        }
    if status and status not in _STATUS_OK_CODES:
        msg = headers.get("X-Api-Message") or ""
        logger.info(
            "[ASR/doubao] req=%s audio_ms=%d elapsed_ms=%d failed http=%s status=%s msg=%r",
            reqid, _duration_ms(pcm_bytes, sample_rate), elapsed_ms, http_code, status, msg[:80],
        )
        return {
            "text": "",
            "http_code": http_code,
            "elapsed_ms": elapsed_ms,
            "business_code": status,
            "message": msg or f"豆包 ASR 失败 status={status}",
        }

    text = "" if status == STATUS_SILENCE else _extract_text(resp)
    logger.info(
        "[ASR/doubao] req=%s audio_ms=%d elapsed_ms=%d status=%s text=%r",
        reqid, _duration_ms(pcm_bytes, sample_rate), elapsed_ms, status or "20000000", text[:40],
    )
    return {
        "text": text,
        "http_code": http_code,
        "elapsed_ms": elapsed_ms,
        "business_code": status or STATUS_OK,
        "message": "",
    }
