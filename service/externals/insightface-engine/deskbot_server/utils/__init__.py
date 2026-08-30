# === externals/insightface-engine 本地副本（insightface-engine 独立化，勿直接删改此段）===
# 来源: src/deskbot_server/utils/__init__.py
# 同步: 主服务改动后按 docs/external_services.md「insightface-engine」节同步本副本并更新日期
# synced: 2026-08-30
# 分叉点: 主服务 utils/__init__ 会 re-export async_helpers/env/logging_setup/singleton，
#         本副本只打包了 paths（运行子集），其余未随副本打包 → 仅暴露路径常量
"""通用工具（运行子集：仅 paths）。"""

from deskbot_server.utils.paths import DATA_DIR, DEFAULT_CONFIG_PATH, ENV_FILE, MODELS_DIR, PROJECT_ROOT, PROMPTS_DIR

__all__ = [
    "DATA_DIR",
    "DEFAULT_CONFIG_PATH",
    "ENV_FILE",
    "MODELS_DIR",
    "PROJECT_ROOT",
    "PROMPTS_DIR",
]
