# === externals/funasr 本地副本（funasr 独立化，勿直接删改此段）===
# 来源: src/deskbot_server/utils/paths.py
# 同步: 主服务改动后按 docs/external_services.md「funasr」节同步本副本并更新日期
# synced: 2026-08-30
# 分叉点: 与源文件差异仅此一处（parents[3]→parents[2]），重新同步时手工重放
"""项目根目录与静态资源路径（src 布局下统一由此解析）。"""

from __future__ import annotations

from pathlib import Path

# externals/funasr/ 独立服务根（含本服务 config.yaml、models/）
# deskbot_server/utils/paths.py → parents[2] == externals/funasr
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
MODELS_DIR = PROJECT_ROOT / "models"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_FILE = PROJECT_ROOT / ".env"
