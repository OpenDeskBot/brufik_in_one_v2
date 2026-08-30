# === externals/funasr 本地副本（funasr 独立化，勿直接删改此段）===
# 来源: src/deskbot_server/model/__init__.py
# 同步: 主服务改动后按 docs/external_services.md「funasr」节同步本副本并更新日期
# synced: 2026-08-30
# 分叉点: 与源文件差异仅此一处——只 re-export AppSettings，裁掉 chat/pb_seq（funasr 不需要），重新同步时手工重放
"""数据模型层：纯数据定义（dataclass / Enum），零业务逻辑、零 IO。"""

from deskbot_server.model.settings import AppSettings

__all__ = [
    "AppSettings",
]
