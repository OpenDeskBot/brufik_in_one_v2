"""清洗存量会话历史：把被思维链污染的 assistant 行恢复成口语正文。

背景
----
``chat_flow`` 归档会话时曾用 ``(reply_text or "").strip() or llm_turn.answer``
兜底，于是静默轮（need_reply=false / 纯动作）把模型的**原始输出**（中文推理 +
``{"need_reply": false, "tts": ""}``）整段写进了 ``device_session_message``。
模型下一轮读到连续几十条「推理 + 沉默」的历史样例，就会照抄——形成
「读到自己的沉默 → 继续沉默」的自我强化，同时动作字段也随格式退化一起丢失。

代码侧已在 ``chat_flow.run_chat_turn`` 修好（只写 ``strip_llm_preamble(reply_text)``，
空则不写 assistant 行）。本脚本负责清洗**已经写脏的存量行**。

规则
----
- assistant 行：``strip_llm_preamble`` 取出 tts；非空则改写为 tts，空则**删行**
  （静默轮在历史里不留痕）
- user 行：以 ``[系统`` 开头的按 ``_session_user_text`` 压缩成紧凑标识
- 孤儿 assistant（前一条不是 user）：删除

用法（在 service/ 目录）
-----------------------
    PYTHONPATH=src .venv/bin/python scripts/clean_session_history.py            # 预演（默认）
    PYTHONPATH=src .venv/bin/python scripts/clean_session_history.py --apply    # 落库

不做成启动时自动迁移——静默改写生产数据风险不可控，必须人工确认后再 --apply。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deskbot_server.db.engine import default_db_path  # noqa: E402
from deskbot_server.infrastructure.llm.utils import strip_llm_preamble  # noqa: E402
from deskbot_server.service.application.chat_flow import _session_user_text  # noqa: E402


def _plan(conn: sqlite3.Connection) -> tuple[list[tuple[int, str]], list[int]]:
    """返回 (改写列表 [(id, 新内容)], 删除 id 列表)。只读。"""
    rows = conn.execute(
        "SELECT id, session_id, role, content FROM device_session_message ORDER BY session_id, id"
    ).fetchall()

    rewrites: list[tuple[int, str]] = []
    deletes: list[int] = []
    prev: dict[str, tuple[str, str]] = {}  # session_id -> (role, content)

    for mid, sid, role, content in rows:
        text = str(content or "").strip()
        if role == "assistant":
            clean = strip_llm_preamble(text)
            last_role, _ = prev.get(sid, ("", ""))
            if last_role != "user":
                deletes.append(mid)  # 孤儿 assistant
            elif not clean:
                deletes.append(mid)  # 静默轮不再留痕
            elif clean != text:
                rewrites.append((mid, clean))
            prev[sid] = ("assistant", clean or text)
        else:
            clean = _session_user_text(text, is_system_round=text.startswith("[系统"))
            if clean != text:
                rewrites.append((mid, clean))
            prev[sid] = ("user", clean)

    return rewrites, deletes


def main() -> int:
    ap = argparse.ArgumentParser(description="清洗被思维链污染的会话历史")
    ap.add_argument("--apply", action="store_true", help="真正写库（缺省只预演）")
    ap.add_argument("--db", default=None, help="数据库路径（缺省 data/opendesk.db）")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else default_db_path()
    if not db_path.is_file():
        print(f"[x] 数据库不存在: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        total = conn.execute("SELECT count(*) FROM device_session_message").fetchone()[0]
        polluting = conn.execute(
            "SELECT count(*) FROM device_session_message WHERE role='assistant' AND content LIKE '%need_reply%'"
        ).fetchone()[0]
        rewrites, deletes = _plan(conn)

        print(f"数据库    : {db_path}")
        print(f"消息总数  : {total}")
        print(f"含 need_reply 的 assistant 行: {polluting}")
        print(f"拟改写    : {len(rewrites)}")
        print(f"拟删除    : {len(deletes)}（静默轮 / 孤儿 assistant）")

        for mid, new in rewrites[:5]:
            print(f"  [改写 #{mid}] {new[:70]}")
        for mid in deletes[:5]:
            old = conn.execute("SELECT content FROM device_session_message WHERE id=?", (mid,)).fetchone()[0]
            print(f"  [删除 #{mid}] {str(old)[:70]}")

        if not args.apply:
            print("\n预演结束。确认无误后加 --apply 落库。")
            return 0

        with conn:
            # 注意参数顺序：rewrites 是 (id, 新内容)，SQL 里 content 在前
            conn.executemany(
                "UPDATE device_session_message SET content=? WHERE id=?",
                [(new, mid) for mid, new in rewrites],
            )
            conn.executemany("DELETE FROM device_session_message WHERE id=?", [(i,) for i in deletes])
        print(f"\n已落库：改写 {len(rewrites)} 行，删除 {len(deletes)} 行。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
