"""从最近一次 VAD 语音注册声纹档案（样本来自注册样本槽，不上传录音）。"""

from __future__ import annotations

from typing import Any

from deskbot_server.service.application.voice_snapshot_cache import take_voice_sample

DEFAULT_SAMPLE_MAX_AGE_S = 60.0


def register_voice_for_device(
    device_id: str,
    name: str,
    *,
    max_age_s: float | None = None,
) -> dict[str, Any]:
    """用「最近一次成功抽出 embedding 的 VAD 语音」注册/更新声纹档案。

    供 LLM 工具 register_voiceprint 与调试页 /api/voice_profiles/register 共用；
    无可用样本（未开启识别 / 还没说过话 / 样本过期）时抛 ValueError（文案面向用户）。
    """
    from deskbot_server.service.voiceprint_service import VoiceprintService

    device_id = str(device_id or "").strip()
    name = str(name or "").strip()
    if not device_id:
        raise ValueError("device_id required")
    if not name:
        raise ValueError("name required")

    svc = VoiceprintService()
    if not svc.enabled():
        raise ValueError(
            "声纹识别未开启：请先在「机器人设置」中开启声纹识别（vpr），"
            "并确保 wespeaker 独立服务已启动"
        )
    try:
        cap = float(max_age_s) if max_age_s is not None else svc.runtime.sample_max_age_s
    except (TypeError, ValueError):
        cap = DEFAULT_SAMPLE_MAX_AGE_S

    embedding = take_voice_sample(device_id, max_age_s=cap)
    if embedding is None:
        raise ValueError(
            "还没有可用的声音样本：请先对机器人说一句完整的话（半秒以上），"
            "再说“记住我的声音，我叫……”"
        )

    profile = svc.register_voice_embedding(name, embedding, device_id=device_id)
    return {"ok": True, "profile": profile, "device_id": device_id}
