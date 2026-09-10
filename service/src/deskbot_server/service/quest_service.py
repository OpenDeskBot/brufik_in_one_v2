"""剧本任务引擎：剧本文件管理 + 任务实例状态机 + complete_task 工具函数。

设计契约（与后台模块编辑器一一对应）：
- 剧本 = JSON 定义文件（data/quest/<name>.json），只存"定义"
  （id/type/prompt/next_task_ids/pos/created_at/updated_at）
- 实例 = 每设备每任务的运行态（DB quest_instance 表）
- 任务类型：
  - once（一次性）：完成（complete_task）后置 completed，并激活其 next_task_ids 全部后继
  - daily（日常）：一旦 running 永续；每次完成向 per-user 的今日 done_list 记账（每用户每日一次）
  - long_term（长期）：一旦 running 永续；每次实质进展向 per-user 的 user_info 记账（累计）
- 状态机：not_started --(入口自动激活 / 前置 once 完成)--> running --(once complete)--> completed
  （completed 只对一次性任务可达且不可再变；日常/长期没有终态）
- 激活规则：无入边（不被任何任务 next_task_ids 引用）的任务在分配剧本时直接 running；
  其余 not_started，待前置一次性任务完成时激活
- 工具函数（供 LLM tool loop 与后台模拟共用）：
  complete_task(device_id, playbook, task_id, user, reason)
  once → 置 completed 并激活后继；daily → done_list 记账；long_term → user_info 记账
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deskbot_server.dao import device_mapper, quest_mapper
from deskbot_server.dao.user_social_store import (
    append_quest_daily_line,
    append_quest_progress_line,
    validate_user_name,
)
from deskbot_server.utils.paths import DATA_DIR
from deskbot_server.utils.singleton import SingletonMeta

logger = logging.getLogger("deskbot-server")

# ── 任务状态常量（三态）──────────────────────────────────────
STATUS_NOT_STARTED = "not_started"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
ALL_STATUS = (STATUS_NOT_STARTED, STATUS_RUNNING, STATUS_COMPLETED)

# ── 任务类型 ────────────────────────────────────────────────
TYPE_ONCE = "once"
TYPE_DAILY = "daily"
TYPE_LONG_TERM = "long_term"
ALL_TYPES = (TYPE_ONCE, TYPE_DAILY, TYPE_LONG_TERM)
TYPE_LABELS = {TYPE_ONCE: "一次性", TYPE_DAILY: "日常", TYPE_LONG_TERM: "长期"}
# 展示/推送排序优先级：once 先（可收口），long_term 次，daily 永续任务沉底
_TYPE_ORDER = {TYPE_ONCE: 0, TYPE_LONG_TERM: 1, TYPE_DAILY: 2}

# system prompt / 主动轮最多列出的进行中任务数
MAX_PROMPT_TASKS = 3

# devices.quest_id 为空时默认绑定的剧本（须与 data/quest/ 下文件一致）
DEFAULT_QUEST_ID = "xiaoy"

PLAYBOOK_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_.\-]{1,64}$")

_DEFAULT_NODE_W = 210
_DEFAULT_NODE_H = 120


class QuestError(Exception):
    """剧本任务错误：定义校验失败 / 状态机非法操作，抛给调用方（工具/API）。"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return str(uuid.uuid4())


# 设计后台模拟用的固定沙箱设备（编辑器不涉及真实设备）
DESIGN_SANDBOX_DEVICE = "__design__"

# ── 剧本文件目录（测试可重定向）────────────────────────────────
_default_playbooks_dir = DATA_DIR / "quest"


def configure_playbooks_dir(path: str | Path) -> None:
    """测试用：把剧本目录指到临时目录。"""
    global _default_playbooks_dir
    _default_playbooks_dir = Path(path)


def _playbooks_dir() -> Path:
    return _default_playbooks_dir


# ── 定义校验（纯函数）──────────────────────────────────────────


def _errs_for_task(task: dict) -> list[str]:
    errs: list[str] = []
    tid = str(task.get("id") or "")
    label = f"任务 {tid or '<无id>'}"
    if not tid or not TASK_ID_RE.match(tid):
        errs.append(f"{label}: id 非法（{tid!r}，需匹配 {TASK_ID_RE.pattern}）")
    ttype = task.get("type")
    if ttype not in ALL_TYPES:
        errs.append(f"{label}: type 必须是一次性/日常/长期之一（{ttype!r}）")
    if not str(task.get("prompt") or "").strip():
        errs.append(f"{label}: prompt 不能为空")
    nxt = task.get("next_task_ids")
    if nxt is None:
        pass  # 缺省由 normalize 补 []；仅当存在但形状错时报错
    elif not isinstance(nxt, list):
        errs.append(f"{label}: next_task_ids 必须是字符串数组")
    else:
        seen: set[str] = set()
        for rid in nxt:
            rid_s = str(rid).strip() if isinstance(rid, str) else ""
            if not isinstance(rid, str) or not rid_s:
                errs.append(f"{label}: next_task_ids 里的后继必须是任务 id 字符串")
                continue
            if not TASK_ID_RE.match(rid_s):
                errs.append(f"{label}: 后继 id 非法（{rid_s!r}）")
                continue
            if rid_s == tid:
                errs.append(f"{label}: 不能把自身设为后继")
                continue
            if rid_s in seen:
                errs.append(f"{label}: next_task_ids 后继 {rid_s} 重复")
            seen.add(rid_s)
    return errs


def validate_playbook(data: Any) -> list[str]:
    """校验整个剧本定义，返回错误列表（空 = 通过）。

    检查：name 合法性、tasks 非空、任务字段、后继引用存在、连边成环。
    """
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["剧本必须是 JSON 对象"]
    name = str(data.get("name") or "")
    if not PLAYBOOK_NAME_RE.match(name):
        errs.append(f"name 非法（{name!r}，需匹配 {PLAYBOOK_NAME_RE.pattern}）")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return errs + ["tasks 必须是列表"]
    ids: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            errs.append("tasks 里存在非对象元素")
            continue
        errs += _errs_for_task(t)
        tid = str(t.get("id") or "")
        if tid:
            if tid in ids:
                errs.append(f"任务 id 重复：{tid}")
            ids.add(tid)
    # 后继引用必须指向存在的任务（允许前向引用）
    edges: list[tuple[str, str]] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        for rid in t.get("next_task_ids") or []:
            rid_s = str(rid).strip() if isinstance(rid, str) else ""
            if rid_s and rid_s not in ids:
                errs.append(f"任务 {tid}: next_task_ids 引用了不存在的任务 {rid_s}")
            if rid_s and tid and rid_s != tid:
                edges.append((tid, rid_s))
    # 环检测（next_task_ids 单端口构图）
    if _has_cycle(edges, ids):
        errs.append("后继关系成环（剧本必须是有向无环图）")
    return errs


def _has_cycle(edges: list[tuple[str, str]], nodes: set[str]) -> bool:
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    visiting: set[str] = set()
    done: set[str] = set()

    def dfs(n: str) -> bool:
        if n in done:
            return False
        if n in visiting:
            return True
        visiting.add(n)
        for nxt in adj.get(n, []):
            if dfs(nxt):
                return True
        visiting.discard(n)
        done.add(n)
        return False

    return any(dfs(n) for n in nodes)


def _default_pos(existing: list[dict]) -> dict:
    """新任务默认摆放：按已有任务数网格排布。"""
    idx = len(existing)
    return {
        "x": 120 + (idx % 4) * 280,
        "y": 120 + (idx // 4) * 220,
        "width": _DEFAULT_NODE_W,
        "height": _DEFAULT_NODE_H,
    }


def _normalize_ids(raw: Any) -> list[str]:
    """next_task_ids 归一：字符串列表、strip、去空、去重保序。"""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        val = item.strip()
        if val and val not in out:
            out.append(val)
    return out


def _normalize_pos(raw: Any, existing: list[dict]) -> dict:
    if not isinstance(raw, dict):
        return _default_pos(existing)
    base = _default_pos(existing)
    for key in ("x", "y", "width", "height"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            base[key] = int(val)
    return base


# 旧格式字段（v1：goal/activation_score/on_success…）——读侧升级时检测用
_LEGACY_TASK_KEYS = (
    "goal",
    "strategy",
    "title",
    "activation_score",
    "initial_status",
    "success_condition",
    "failure_condition",
    "on_success",
    "on_failure",
    "score_sources",
)


def normalize_task(raw: dict, *, existing: list[dict] | None = None) -> dict:
    """补全任务默认字段并丢弃未知/遗留键（校验交给 validate_playbook）。

    兼容旧字段：prompt 缺失且 goal 非空 → goal 迁移为 prompt；
    next_task_ids 缺失且 on_success 存在 → 取其任务 id（分数丢弃）。
    """
    existing = existing or []
    now = _utcnow_iso()
    raw_goal = str(raw.get("goal") or "").strip()
    raw_prompt = str(raw.get("prompt") or "").strip()
    prompt = raw_prompt or raw_goal  # 旧 goal → prompt 迁移
    nxt: Any = raw.get("next_task_ids")
    if nxt is None and raw.get("on_success") is not None:
        nxt = [r.get("id") for r in raw.get("on_success") or [] if isinstance(r, dict)]
    return {
        "id": str(raw.get("id") or "").strip(),
        "type": str(raw.get("type") or TYPE_ONCE).strip()
        if str(raw.get("type") or "").strip() in ALL_TYPES
        else TYPE_ONCE,
        "prompt": prompt.strip(),
        "next_task_ids": _normalize_ids(nxt),
        "pos": _normalize_pos(raw.get("pos"), existing),
        "created_at": str(raw.get("created_at") or now).strip() or now,
        "updated_at": str(raw.get("updated_at") or now).strip() or now,
    }


def _has_legacy_fields(tasks: list[dict]) -> bool:
    return any(isinstance(t, dict) and any(k in t for k in _LEGACY_TASK_KEYS) for t in tasks)


# ── 剧本任务引擎 ──────────────────────────────────────────────


class QuestService(metaclass=SingletonMeta):
    """剧本文件管理 + 任务实例状态机 + complete_task 工具函数（无状态，状态全在文件/DB）。"""

    # ── 剧本文件管理 ──────────────────────────────────────────

    @staticmethod
    def _playbook_path(name: str) -> Path:
        if not PLAYBOOK_NAME_RE.match(name):
            raise QuestError(f"非法剧本名: {name!r}")
        return _playbooks_dir() / f"{name}.json"

    def list_playbooks(self) -> list[str]:
        d = _playbooks_dir()
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json") if p.is_file())

    def get_playbook(self, name: str) -> dict | None:
        """读剧本；旧格式文件在内存升级为新形状（不自动落盘，下次保存写出）。"""
        path = self._playbook_path(name)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QuestError(f"剧本 {name} 读取失败: {exc}") from exc
        if isinstance(data, dict) and _has_legacy_fields(data.get("tasks") or []):
            tasks: list[dict] = []
            for t in data.get("tasks") or []:
                tasks.append(normalize_task(t, existing=tasks))
            data = dict(data)
            data["tasks"] = tasks
        return data

    def create_playbook(self, name: str) -> dict:
        if not PLAYBOOK_NAME_RE.match(name):
            raise QuestError(f"非法剧本名: {name!r}（需匹配 {PLAYBOOK_NAME_RE.pattern}）")
        if self.get_playbook(name) is not None:
            raise QuestError(f"剧本已存在: {name}")
        data = {"name": name, "tasks": []}
        self._write_playbook(name, data)
        return data

    def save_playbook(self, name: str, data: dict) -> dict:
        """整体保存（导入用）：升级旧字段→校验通过后原子写盘。"""
        data = dict(data or {})
        data["name"] = name
        if isinstance(data.get("tasks"), list) and _has_legacy_fields(data["tasks"]):
            upgraded: list[dict] = []
            for t in data["tasks"]:
                upgraded.append(normalize_task(t, existing=upgraded))
            data["tasks"] = upgraded
        errs = validate_playbook(data)
        if errs:
            raise QuestError("剧本校验失败：" + "；".join(errs))
        tasks: list[dict] = []
        for raw in data.get("tasks") or []:
            tasks.append(normalize_task(raw, existing=tasks))
        data["tasks"] = tasks
        self._write_playbook(name, data)
        return data

    def delete_playbook(self, name: str) -> None:
        quest_mapper.delete_by_playbook(name)
        path = self._playbook_path(name)
        if path.is_file():
            path.unlink()

    def _write_playbook(self, name: str, data: dict) -> None:
        path = self._playbook_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ── 任务 CRUD（后台编辑器用）──────────────────────────────

    def add_task(self, name: str, raw: dict) -> dict:
        pb = self._require_playbook(name)
        tasks = pb.setdefault("tasks", [])
        task = normalize_task(raw, existing=tasks)
        if not task["id"]:
            raise QuestError("任务缺少 id")
        if any(t["id"] == task["id"] for t in tasks):
            raise QuestError(f"任务 id 已存在: {task['id']}")
        tasks.append(task)
        self.save_playbook(name, pb)
        return task

    def update_task(self, name: str, task_id: str, patch: dict) -> dict:
        """更新任务字段；支持改名（id 变了会同步其他任务的后继引用和运行实例）。"""
        pb = self._require_playbook(name)
        tasks = pb.get("tasks") or []
        target = next((t for t in tasks if t["id"] == task_id), None)
        if target is None:
            raise QuestError(f"任务不存在: {task_id}")
        raw_id = patch.get("id")
        new_id = str(raw_id).strip() if raw_id is not None else task_id
        renamed = new_id != task_id
        if renamed:
            if not TASK_ID_RE.match(new_id):
                raise QuestError(f"任务 id 非法（{new_id!r}，需匹配 {TASK_ID_RE.pattern}）")
            if any(t["id"] == new_id for t in tasks):
                raise QuestError(f"任务 id 已存在: {new_id}")
        merged = dict(target)
        for key, val in patch.items():
            if key == "id":
                continue
            if key == "pos":
                merged[key] = _normalize_pos(val, tasks)
            elif key == "next_task_ids":
                merged[key] = _normalize_ids(val)
            elif key in ("type", "prompt"):
                merged[key] = val
            # 其余键（含旧字段）忽略
        merged = normalize_task(merged, existing=tasks)
        merged["id"] = new_id
        merged["updated_at"] = _utcnow_iso()  # 内容变更刷新
        tasks[tasks.index(target)] = merged
        if renamed:
            # 其他任务的后继引用里指向旧 id 的同步改名，并去重
            for t in tasks:
                if t is merged:
                    continue
                out: list[str] = []
                for rid in t.get("next_task_ids") or []:
                    rid_new = new_id if rid == task_id else rid
                    if rid_new not in out:
                        out.append(rid_new)
                t["next_task_ids"] = out
        self.save_playbook(name, pb)
        if renamed:
            for row in quest_mapper.list_by_playbook(name):
                if row.task_id == task_id:
                    quest_mapper.rename_instance(row.device_id, name, task_id, new_id)
        return merged

    def delete_task(self, name: str, task_id: str) -> None:
        pb = self._require_playbook(name)
        tasks = pb.get("tasks") or []
        if not any(t["id"] == task_id for t in tasks):
            raise QuestError(f"任务不存在: {task_id}")
        pb["tasks"] = [t for t in tasks if t["id"] != task_id]
        # 清理其他任务的后继引用里指向被删任务的 id
        for t in pb["tasks"]:
            t["next_task_ids"] = [rid for rid in t.get("next_task_ids") or [] if rid != task_id]
        self.save_playbook(name, pb)
        self._delete_task_instances(name, task_id)

    def set_edge(self, name: str, from_id: str, to_id: str) -> dict:
        """连边：from 的后继 → to（单端口，无分数）。重复连 = 幂等 no-op。"""
        pb = self._require_playbook(name)
        tasks = pb.get("tasks") or []
        src = next((t for t in tasks if t["id"] == from_id), None)
        if src is None:
            raise QuestError(f"任务不存在: {from_id}")
        if not any(t["id"] == to_id for t in tasks):
            raise QuestError(f"任务不存在: {to_id}")
        if from_id == to_id:
            raise QuestError("不能把自身设为后继")
        nxt = src.get("next_task_ids") or []
        if to_id not in nxt:
            nxt = nxt + [to_id]
            src["next_task_ids"] = nxt
            src["updated_at"] = _utcnow_iso()
            self.save_playbook(name, pb)
        return {"from": from_id, "to": to_id}

    def remove_edge(self, name: str, from_id: str, to_id: str) -> None:
        pb = self._require_playbook(name)
        tasks = pb.get("tasks") or []
        src = next((t for t in tasks if t["id"] == from_id), None)
        if src is None:
            raise QuestError(f"任务不存在: {from_id}")
        nxt = [rid for rid in src.get("next_task_ids") or [] if rid != to_id]
        if len(nxt) != len(src.get("next_task_ids") or []):
            src["next_task_ids"] = nxt
            src["updated_at"] = _utcnow_iso()
            self.save_playbook(name, pb)

    def _delete_task_instances(self, name: str, task_id: str) -> None:
        rows = quest_mapper.list_by_playbook(name)
        for row in rows:
            if row.task_id == task_id:
                quest_mapper.delete_instance(row.device_id, name, task_id)

    def _require_playbook(self, name: str) -> dict:
        pb = self.get_playbook(name)
        if pb is None:
            raise QuestError(f"剧本不存在: {name}")
        return pb

    # ── 实例管理（分配/查询/重置）────────────────────────────

    @staticmethod
    def _entry_task_ids(pb: dict) -> set[str]:
        """无入边任务集：不被任何任务的 next_task_ids 引用。"""
        referenced: set[str] = set()
        for t in pb.get("tasks") or []:
            referenced.update(t.get("next_task_ids") or [])
        return {t["id"] for t in pb.get("tasks") or [] if t["id"] not in referenced}

    def ensure_instances(self, device_id: str, playbook_name: str) -> dict:
        """为设备创建剧本下缺失的任务实例（幂等）。

        入口任务（无任何前置连线）直接 running（剧情起点），其余 not_started。
        """
        pb = self._require_playbook(playbook_name)
        existing = {r.task_id for r in quest_mapper.list_instances(device_id, playbook_name)}
        now = _utcnow_iso()
        entry_ids = self._entry_task_ids(pb)
        created = activated = 0
        for t in pb.get("tasks") or []:
            tid = t["id"]
            if tid in existing:
                continue
            is_entry = tid in entry_ids
            status = STATUS_RUNNING if is_entry else STATUS_NOT_STARTED
            quest_mapper.insert_instance(
                id=_new_id(),
                device_id=device_id,
                playbook=playbook_name,
                task_id=tid,
                status=status,
                started_at=now if is_entry else None,
                finished_at=None,
                result=None,
                created_at=now,
                updated_at=now,
            )
            created += 1
            if is_entry:
                activated += 1
        return {"created": created, "activated": activated, "total": len(pb.get("tasks") or [])}

    def reset_instances(self, device_id: str, playbook_name: str) -> dict:
        """清空并重建设备在剧本下的全部实例（后台测试用）。"""
        quest_mapper.delete_instances(device_id, playbook_name)
        return self.ensure_instances(device_id, playbook_name)

    def get_instances(self, device_id: str, playbook_name: str) -> list[dict]:
        rows = quest_mapper.list_instances(device_id, playbook_name)
        return [_instance_to_dict(r) for r in rows]

    def get_task_definition(self, playbook_name: str, task_id: str) -> dict | None:
        pb = self._require_playbook(playbook_name)
        return next((t for t in pb.get("tasks") or [] if t["id"] == task_id), None)

    # ── 设备视角查询（运行时接线用）────────────────────────────

    def get_bound_playbook(self, device_id: str) -> str | None:
        """设备绑定的剧本名（devices.quest_id）。

        设备行不存在 / quest_id 空 / 剧本文件不存在或读取失败（QuestError）→ None。
        """
        dev = device_mapper.get_by_device_id(device_id)
        if dev is None:
            return None
        qid = str(dev.quest_id or "").strip()
        if not qid:
            return None
        try:
            if self.get_playbook(qid) is None:
                return None
        except QuestError:
            return None
        return qid

    def _ensure_default_quest_binding(self, device_id: str) -> None:
        """devices.quest_id 为空 → 惰性绑定默认剧本（``DEFAULT_QUEST_ID``）。

        仅在默认剧本文件可读时落库（缺文件不写脏绑定，注入自然为空）；
        设备行不存在 / 已有显式绑定（含绑定到其它剧本）→ 不动。
        """
        dev = device_mapper.get_by_device_id(str(device_id or "").strip())
        if dev is None or str(dev.quest_id or "").strip():
            return
        try:
            if self.get_playbook(DEFAULT_QUEST_ID) is None:
                return
        except QuestError:
            return
        try:
            device_mapper.update_quest_id(str(device_id).strip(), DEFAULT_QUEST_ID)
            logger.info("[quest] 设备未绑定剧本，默认绑定 %s device_id=%s", DEFAULT_QUEST_ID, device_id)
        except Exception:
            logger.debug("[quest] 默认绑定失败（忽略） device_id=%s", device_id, exc_info=True)

    def _activate_entry_rows(self, device_id: str, playbook: str) -> None:
        """把「not_started 但当前定义无入边」的遗留行补激活为 running。

        旧库升级 / 编辑器给运行中设备剧本新增入口任务后的兜底；幂等。
        **不放 ensure_instances**：沙箱 /state 手动置 not_started 的演示不能被弹回。
        """
        pb = self._require_playbook(playbook)
        entry_ids = self._entry_task_ids(pb)
        if not entry_ids:
            return
        now = _utcnow_iso()
        for row in quest_mapper.list_instances(device_id, playbook):
            if row.status == STATUS_NOT_STARTED and row.task_id in entry_ids:
                quest_mapper.update_instance(
                    id=row.id,
                    status=STATUS_RUNNING,
                    started_at=now,
                    finished_at=None,
                    result=row.result,
                    updated_at=now,
                )
                logger.info("[quest] 入口任务补激活 task_id=%s device_id=%s", row.task_id, device_id)

    def get_current_tasks(self, device_id: str) -> list[dict]:
        """设备当前进行中（running）的任务 —— 仅绑定剧本（devices.quest_id）。

        分支语义：
        0. quest_id 为空 → 尝试默认绑定 ``DEFAULT_QUEST_ID``（xiaoy）
        1. 设备行不存在 / 仍未绑定 / 剧本文件缺失 → []
        2. 缺实例 → ensure_instances 幂等补齐（含入口自动激活）；
           再 _activate_entry_rows 补激活旧库遗留的入口行
        3. 返回 running 列表（type+prompt，按 once→long_term→daily、组内定义序排序）
        """
        self._ensure_default_quest_binding(device_id)
        playbook = self.get_bound_playbook(device_id)
        if not playbook:
            return []
        try:
            self.ensure_instances(device_id, playbook)
            self._activate_entry_rows(device_id, playbook)
        except QuestError:
            return []  # 检查后剧本文件被删的竞态兜底
        return self._current_tasks_for_playbook(device_id, playbook)

    def _current_tasks_for_playbook(self, device_id: str, playbook: str) -> list[dict]:
        """单个剧本下 running 任务的活跃目标集（type+prompt，排序见 docstring）。"""
        pb = self.get_playbook(playbook)
        if pb is None:
            return []
        by_id: dict[str, dict] = {}
        order: list[str] = []
        for t in pb.get("tasks") or []:
            tid = t["id"]
            by_id[tid] = t
            order.append(tid)
        rows = {r.task_id: r for r in quest_mapper.list_instances(device_id, playbook)}
        out: list[tuple[int, int, dict]] = []
        for idx, tid in enumerate(order):
            row = rows.get(tid)
            if row is None or row.status != STATUS_RUNNING:
                continue
            defn = by_id[tid]
            out.append(
                (
                    _TYPE_ORDER.get(str(defn.get("type") or ""), 2),
                    idx,
                    {
                        "playbook": playbook,
                        "task_id": tid,
                        "type": defn.get("type", TYPE_ONCE),
                        "prompt": str(defn.get("prompt") or "").strip(),
                    },
                )
            )
        out.sort(key=lambda x: (x[0], x[1]))
        return [item[2] for item in out]

    def get_tool_calls(self, device_id: str) -> list[dict]:
        """设备当前可用的剧情工具调用契约（供 LLM tool loop schema 注入）。

        无 running 任务 → []（不向模型广告不可用工具）。
        """
        tasks = self.get_current_tasks(device_id)
        if not tasks:
            return []
        return [
            {
                "name": "complete_task",
                "description": (
                    "完成/记录进行中剧情任务的推进。"
                    "一次性任务：目标达成后调用即置完成并自动接续其后继；"
                    "日常任务：对该用户今日完成一次即可调一次（已记录会返回 deduped）；"
                    "长期任务：有实质新进展才调用，多次调用按用户/时间累计记录。"
                ),
                "parameters": {
                    "task_id": "string（目标任务 id）",
                    "user": "string（当前对话用户；日常/长期任务必填）",
                    "reason": "string（完成内容/达成结果/新进展，口语转述，必填）",
                },
                "available_task_ids": [t["task_id"] for t in tasks],
                "tasks": [{"task_id": t["task_id"], "type": t["type"]} for t in tasks],
            }
        ]

    # ── 任务完成（LLM 工具函数与后台模拟共用）──────────────────

    def complete_task(
        self, device_id: str, playbook_name: str, task_id: str, *, user: str, reason: str
    ) -> dict:
        """工具函数：完成任务并落地（一次性置终态+激活后继 / 日常记账 / 长期记账）。

        - once：实例 running 才能完成；置 completed（finished_at/result=reason），
          并沿 next_task_ids 激活后继（缺失实例自动补建 running）
        - daily：user 必填；向该用户今日 done_list 追加 ``[{task_id}] reason`` 行；
          今日已含该任务行 → 不写返回 deduped=True（幂等）；实例保持 running
        - long_term：user 必填；向该用户 user_info 追加进展行（累计）；实例保持 running
        """
        reason_txt = str(reason or "").strip()
        if not reason_txt:
            raise QuestError("缺少完成原因（reason），请写达成内容/结果描述")
        defn = self.get_task_definition(playbook_name, task_id)
        if defn is None:
            raise QuestError(f"任务定义不存在: {playbook_name}/{task_id}")
        ttype = str(defn.get("type") or TYPE_ONCE)
        if ttype not in ALL_TYPES:
            raise QuestError(f"任务类型非法: {ttype!r}")
        row = self._require_instance(device_id, playbook_name, task_id)
        if row.status == STATUS_NOT_STARTED:
            raise QuestError(f"任务未激活（{task_id}），还不能完成")
        if row.status == STATUS_COMPLETED:
            raise QuestError(f"任务已完成（{task_id}），不能重复完成")

        now = _utcnow_iso()
        if ttype == TYPE_ONCE:
            quest_mapper.update_instance(
                id=row.id,
                status=STATUS_COMPLETED,
                started_at=_dt_str(row.started_at),
                finished_at=now,
                result=reason_txt,
                updated_at=now,
            )
            activated = self._activate_next_tasks(device_id, playbook_name, task_id, now=now)
            return {
                "type": TYPE_ONCE,
                "task_id": task_id,
                "status": STATUS_COMPLETED,
                "reason": reason_txt,
                "task": self._require_instance_dict(device_id, playbook_name, task_id),
                "activated": activated,
            }
        # daily / long_term：写 per-user 记录，实例保持 running
        vname = self._validated_user(user)
        try:
            if ttype == TYPE_DAILY:
                out = append_quest_daily_line(device_id, vname, task_id, reason_txt)
            else:
                out = append_quest_progress_line(device_id, vname, task_id, reason_txt)
        except (OSError, ValueError) as exc:
            raise QuestError(f"记录写入失败: {exc}") from exc
        return {
            "type": ttype,
            "task_id": task_id,
            "user": vname,
            "reason": reason_txt,
            "status": STATUS_RUNNING,
            "deduped": bool(out.get("deduped")),
            "written": bool(out.get("written")),
            "path": str(out.get("path") or ""),
        }

    def _validated_user(self, user: str) -> str:
        try:
            return validate_user_name(user)
        except ValueError as exc:
            raise QuestError(f"日常/长期任务需要当前对话用户 user（{exc}）") from exc

    def _activate_next_tasks(
        self, device_id: str, playbook_name: str, task_id: str, *, now: str
    ) -> list[dict]:
        """激活任务的后继（complete_task once 分支用）。

        对每个 next_task_ids：无实例 → 补建 running；not_started → running；
        running → 不动；completed → 不复活。
        返回 [{task_id, status, created}]。
        """
        defn = self.get_task_definition(playbook_name, task_id)
        next_ids = (defn or {}).get("next_task_ids") or []
        out: list[dict] = []
        for nid in next_ids:
            tgt = quest_mapper.get_instance(device_id, playbook_name, nid)
            if tgt is None:
                quest_mapper.insert_instance(
                    id=_new_id(),
                    device_id=device_id,
                    playbook=playbook_name,
                    task_id=nid,
                    status=STATUS_RUNNING,
                    started_at=now,
                    finished_at=None,
                    result=None,
                    created_at=now,
                    updated_at=now,
                )
                out.append({"task_id": nid, "status": STATUS_RUNNING, "created": True})
            elif tgt.status == STATUS_NOT_STARTED:
                quest_mapper.update_instance(
                    id=tgt.id,
                    status=STATUS_RUNNING,
                    started_at=now,
                    finished_at=_dt_str(tgt.finished_at),
                    result=tgt.result,
                    updated_at=now,
                )
                out.append({"task_id": nid, "status": STATUS_RUNNING, "created": False})
            # running / completed：不复活、不重置
        return out

    def set_state(
        self, device_id: str, playbook_name: str, task_id: str, status: str, result: str | None = None
    ) -> dict:
        """设计沙箱直接改实例状态（跨过工具契约校验，允许任意跳转，不传播）。

        completed → 写 finished_at/result；running → 无 started_at 补 now 并清终态字段；
        not_started → 清 started_at/finished_at/result（表示从未激活）。
        """
        if status not in ALL_STATUS:
            raise QuestError(f"status 必须是 {ALL_STATUS}（{status!r}）")
        row = self._require_instance(device_id, playbook_name, task_id)
        now = _utcnow_iso()
        started_at = _dt_str(row.started_at)
        finished_at = _dt_str(row.finished_at)
        result_txt = row.result
        if status == STATUS_COMPLETED:
            if not started_at:
                started_at = now
            finished_at = now
            if result is not None:
                result_txt = str(result).strip() or None
        elif status == STATUS_RUNNING:
            if not started_at:
                started_at = now
            finished_at = None
            # 回到进行中：清掉旧完成原因（除非本轮显式带 result）
            result_txt = str(result).strip() or None if result is not None else None
        else:  # not_started：清空运行痕迹
            started_at = None
            finished_at = None
            result_txt = None
        quest_mapper.update_instance(
            id=row.id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            result=result_txt,
            updated_at=now,
        )
        return self._require_instance_dict(device_id, playbook_name, task_id)

    def _require_instance(self, device_id: str, playbook_name: str, task_id: str):
        row = quest_mapper.get_instance(device_id, playbook_name, task_id)
        if row is None:
            raise QuestError(f"任务实例不存在（剧本 {playbook_name}/{task_id}）——请先创建/分配实例")
        return row

    def _require_instance_dict(self, device_id: str, playbook_name: str, task_id: str) -> dict:
        row = quest_mapper.get_instance(device_id, playbook_name, task_id)
        return _instance_to_dict(row) if row else {"task_id": task_id}


def _instance_to_dict(row) -> dict[str, Any]:
    return {
        "device_id": row.device_id,
        "playbook": row.playbook,
        "task_id": row.task_id,
        "status": row.status,
        "started_at": _dt_str(row.started_at),
        "finished_at": _dt_str(row.finished_at),
        "result": row.result,
    }


def _dt_str(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat(timespec="seconds")
    return str(val)
