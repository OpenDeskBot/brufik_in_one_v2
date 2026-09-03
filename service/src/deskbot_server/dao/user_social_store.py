"""按人归档的用户社交文件存储（``data/{device_id}/`` 下三类 txt）。

- ``user_info_{name}.txt``：用户档案（agent 经 ``update_user_info`` 逐条追加）
- ``done_list_{name}_{YYYYMMDD}.txt``：当日主动任务/关心记录（agent 经
  ``update_daily_task`` 记账，文件名日期为北京时区当日）
- ``user_last_talk_{name}.txt``：最近一次对话时刻（服务端每轮用户对话结束打点）

纯模块函数 + OSError/ValueError 兜底（镜像 dao/servo_config_store.py 风格），
不依赖 infra/llm（北京时区逻辑本地实现，避免 dao → infra 反向依赖）。

写入约定：每条记录以 ``YYYY-MM-DD HH:MM:SS `` 服务端时间前缀开头；agent 自带的
datetime 前缀（``-``/``/``/``T`` 分隔）原样保留。行级完全去重；文件超长裁旧保新。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from deskbot_server.utils.device_data import device_data_dir

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

logger = logging.getLogger("deskbot-server")

# 常规用户名：中文/字母/数字/下划线开头，可含空格与 ._-，总长 ≤ 32
USER_NAME_RE = re.compile(r"^[0-9A-Za-z_一-鿿][0-9A-Za-z_一-鿿 ._-]{0,31}$")
# 消息自带时间前缀（- 或 / 或 T 分隔均兼容）
_TS_PREFIX_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}")

# 写侧裁剪（保留最新 N 行；读侧另有行/字符上限，双层有界）
USER_INFO_MAX_LINES = 200
USER_INFO_TRIM_KEEP = 100
DONE_LIST_MAX_LINES = 120
DONE_LIST_TRIM_KEEP = 60


def _beijing_now() -> dt.datetime:
    if ZoneInfo is not None:
        return dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def _beijing_today() -> str:
    return _beijing_now().strftime("%Y%m%d")


def _ts_str(now: dt.datetime | None = None) -> str:
    return (now or _beijing_now()).strftime("%Y-%m-%d %H:%M:%S")


def user_file_stem(name: str) -> str:
    """用户名 → 文件名主干（读/写共用同一函数保证一致）。

    常规名（中文/字母/数字/空格/._-，≤32 字符）原样；非常规名回退
    ``u_<sha1 前 10 位>``，避免路径注入与非法文件系统字符。
    """
    text = str(name or "").strip()
    if not text:
        raise ValueError("用户名为空")
    if len(text) <= 32 and USER_NAME_RE.match(text):
        return text
    return "u_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def validate_user_name(name: str) -> str:
    """工具写入侧校验：strip + 常规名正则，非法抛 ValueError。"""
    text = str(name or "").strip()
    if not text:
        raise ValueError("用户名为空")
    if len(text) > 32 or not USER_NAME_RE.match(text):
        raise ValueError(f"用户名含非法字符或过长: {name!r}（仅支持中文/字母/数字/空格/._-，≤32 字符）")
    return text


def _info_path(device_id: str, name: str) -> Path:
    return device_data_dir(device_id) / f"user_info_{user_file_stem(name)}.txt"


def _done_list_path(device_id: str, name: str) -> Path:
    return device_data_dir(device_id) / f"done_list_{user_file_stem(name)}_{_beijing_today()}.txt"


def _last_talk_path(device_id: str, name: str) -> Path:
    return device_data_dir(device_id) / f"user_last_talk_{user_file_stem(name)}.txt"


# ────────────────── 写入 ─────────────────────────


def _append_line(path: Path, line: str, *, max_lines: int, trim_keep: int) -> dict[str, Any]:
    """追加单行；行级去重；超限裁旧保新。返回 {ok, created, deduped, line_count}。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (line or "").strip()
    if not text:
        raise ValueError("内容为空")
    existed = path.exists()
    lines: list[str] = []
    if existed:
        try:
            lines = [ln.rstrip("\n") for ln in path.read_text(encoding="utf-8").splitlines()]
        except OSError:
            raise
    if text in lines:
        return {"ok": True, "created": False, "deduped": True, "line_count": len(lines), "path": str(path)}
    lines.append(text)
    if len(lines) > max_lines:
        lines = lines[-trim_keep:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "created": not existed, "deduped": False, "line_count": len(lines), "path": str(path)}


def append_user_info_line(device_id: str, name: str, message: str) -> dict[str, Any]:
    """用户档案追加一行（服务端补时间前缀）。name 非法抛 ValueError。"""
    msg = str(message or "").strip()
    if not msg:
        raise ValueError("chat_message 为空")
    return _append_line(
        _info_path(device_id, validate_user_name(name)),
        f"{_ts_str()} {msg}",
        max_lines=USER_INFO_MAX_LINES,
        trim_keep=USER_INFO_TRIM_KEEP,
    )


def append_daily_task_line(device_id: str, name: str, message: str) -> dict[str, Any]:
    """今日 done_list 追加一行。message 自带 datetime 前缀则原样，否则服务端补前缀。"""
    msg = str(message or "").strip()
    if not msg:
        raise ValueError("message 为空")
    if not _TS_PREFIX_RE.match(msg):
        msg = f"{_ts_str()} {msg}"
    return _append_line(
        _done_list_path(device_id, validate_user_name(name)),
        msg,
        max_lines=DONE_LIST_MAX_LINES,
        trim_keep=DONE_LIST_TRIM_KEEP,
    )


def stamp_user_last_talk(device_id: str, name: str) -> str | None:
    """覆盖写单行对话时刻；失败返回 None（不向调用层抛）。"""
    try:
        path = _last_talk_path(device_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = _ts_str()
        path.write_text(ts + "\n", encoding="utf-8")
        return ts
    except (OSError, ValueError) as exc:
        logger.debug("[user_social_store] last_talk 打点失败 device_id=%s name=%r err=%s", device_id, name, exc)
        return None


# ────────────────── 读取 ─────────────────────────


def read_user_last_talk(device_id: str, name: str) -> str | None:
    """读最近对话时间 "YYYY-MM-DD HH:MM:SS"；无文件/空/异常 → None。"""
    try:
        path = _last_talk_path(device_id, name)
        if not path.is_file():
            return None
        first = path.read_text(encoding="utf-8").splitlines()
        ts = (first[0] if first else "").strip()
        return ts or None
    except (OSError, ValueError) as exc:
        logger.debug("[user_social_store] last_talk 读取失败 device_id=%s name=%r err=%s", device_id, name, exc)
        return None


def _read_block_lines(path: Path, *, max_lines: int, max_chars: int) -> tuple[list[str], bool]:
    """读文件保留最新若干行且总字符有界；缺失/异常返回 ([], False)。

    返回 ``(keep_lines, dropped)``：dropped=True 表示原始行数多于保留行数（截断）。
    """
    if not path.is_file():
        return [], False
    try:
        lines = [ln.rstrip("\n").strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        logger.debug("[user_social_store] 读取失败 path=%s err=%s", path, exc)
        return [], False
    lines = [ln for ln in lines if ln]
    if not lines:
        return [], False
    keep: list[str] = []
    total = 0
    for ln in reversed(lines):
        cost = len(ln) + 1
        if keep and total + cost > max_chars:
            break
        keep.append(ln)
        total += cost
        if len(keep) >= max_lines:
            break
    keep.reverse()
    return keep, len(lines) > len(keep)


def read_user_info_block(device_id: str, name: str, *, max_lines: int = 30, max_chars: int = 600) -> str | None:
    """用户档案文本块（时间旧→新）；文件缺失/异常 → None。"""
    try:
        path = _info_path(device_id, name)
    except ValueError:
        return None
    lines, dropped = _read_block_lines(path, max_lines=max_lines, max_chars=max_chars)
    if not lines:
        return None
    body = "\n".join(lines)
    if dropped:
        return f"（记录较多，仅显示最近 {len(lines)} 条，按时间从旧到新）\n{body}"
    return body


def read_done_list_block(device_id: str, name: str, *, max_lines: int = 20, max_chars: int = 400) -> str | None:
    """今日 done_list 文本块；无记录/异常 → None。"""
    try:
        path = _done_list_path(device_id, name)
    except ValueError:
        return None
    lines, _dropped = _read_block_lines(path, max_lines=max_lines, max_chars=max_chars)
    if not lines:
        return None
    return "\n".join(lines)
