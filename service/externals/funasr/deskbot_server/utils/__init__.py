# === externals/funasr 本地副本（funasr 独立化，勿直接删改此段）===
# 来源: src/deskbot_server/utils/__init__.py
# 同步: 主服务改动后按 docs/external_services.md「funasr」节同步本副本并更新日期
# synced: 2026-08-30
# 分叉点: 与源文件差异仅此一处——只 re-export paths 常量，裁掉 async_helpers/env/logging_setup/singleton（funasr 不需要），重新同步时手工重放
"""通用工具：路径与环境辅助（funasr 独立子集）。"""

from deskbot_server.utils.paths import DATA_DIR, DEFAULT_CONFIG_PATH, ENV_FILE, MODELS_DIR, PROJECT_ROOT, PROMPTS_DIR

__all__ = [
    "DATA_DIR",
    "DEFAULT_CONFIG_PATH",
    "ENV_FILE",
    "MODELS_DIR",
    "PROJECT_ROOT",
    "PROMPTS_DIR",
]
