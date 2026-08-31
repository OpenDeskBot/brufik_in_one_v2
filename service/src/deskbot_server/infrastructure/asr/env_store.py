"""读写 .env 中的豆包云 ASR 配置（Seed-ASR 2.0）。

复用 tts.env_store 的通用 .env 读写（read_env_file / update_env_keys），
密钥（DOUBAO_ASR_API_KEY）掩码语义与豆包 TTS 一致：脱敏占位不覆盖已有值。
写入后同步更新 os.environ，使运行中的进程立即生效。
**注意**：前端已改为设备级保存（devices.asr_param），本模块仅作全局兜底读源。
"""

from __future__ import annotations

from deskbot_server.infrastructure.asr.doubao import DEFAULT_RESOURCE_ID, DEFAULT_UID, DEFAULT_URL
from deskbot_server.infrastructure.tts.env_store import read_env_file, update_env_keys
from deskbot_server.utils.env import load_dotenv

DOUBAO_ASR_ENV_KEYS = (
    "DOUBAO_ASR_API_KEY",
    "DOUBAO_ASR_RESOURCE_ID",
    "DOUBAO_ASR_UID",
    "DOUBAO_ASR_URL",
)

# 表单字段名（robot-settings 对话框 / asr_test 覆盖字段共用）→ env 键
_PAYLOAD_FIELD_BY_ENV_KEY = {
    "api_key": "DOUBAO_ASR_API_KEY",
    "resource_id": "DOUBAO_ASR_RESOURCE_ID",
    "uid": "DOUBAO_ASR_UID",
    "url": "DOUBAO_ASR_URL",
}

# 兜底默认值（缺省时展示/构造配置用，与 doubao.py 一致）
_FALLBACK = {
    "DOUBAO_ASR_RESOURCE_ID": DEFAULT_RESOURCE_ID,
    "DOUBAO_ASR_UID": DEFAULT_UID,
    "DOUBAO_ASR_URL": DEFAULT_URL,
}


def _mask_secret(value: str) -> str:
    """头 3 尾 3 + 星号；≤6 位全星号（与 tts/doubao._mask_secret 同模式）。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if len(raw) <= 6:
        return "*" * len(raw)
    return raw[:3] + "*" * (len(raw) - 6) + raw[-3:]


def _is_masked_secret(value: str) -> bool:
    """判断是否为 _mask_secret 产生的占位串，避免误当作真实密钥覆盖。"""
    raw = (value or "").strip()
    if not raw or "*" not in raw:
        return False
    if len(raw) <= 6:
        return all(c == "*" for c in raw)
    return raw[3:-3] == "*" * (len(raw) - 6)


def load_doubao_asr_env() -> dict[str, str]:
    """.env 当前值（6 键），url/uid 缺省时带默认兜底。"""
    env = read_env_file()
    out: dict[str, str] = {}
    for key in DOUBAO_ASR_ENV_KEYS:
        val = (env.get(key) or "").strip()
        if not val and key in _FALLBACK:
            val = _FALLBACK[key]
        out[key] = val
    return out


def save_doubao_asr_env(payload: dict[str, str]) -> None:
    """保存豆包 ASR 配置到 .env 并刷新进程内环境变量（Deprecated：前端已改设备级）。

    留空字段不覆盖已有值；api_key 为掩码占位时同样保留已保存值。
    """
    existing = read_env_file()
    updates: dict[str, str] = {}
    for field, env_key in _PAYLOAD_FIELD_BY_ENV_KEY.items():
        raw = str(payload.get(field) or "").strip()
        if env_key == "DOUBAO_ASR_API_KEY" and _is_masked_secret(raw):
            raw = ""
        if not raw:
            raw = (existing.get(env_key) or "").strip()
        updates[env_key] = raw
    update_env_keys(updates, keys=DOUBAO_ASR_ENV_KEYS, comment="# 豆包云 ASR（Seed-ASR 2.0）")
    load_dotenv()
