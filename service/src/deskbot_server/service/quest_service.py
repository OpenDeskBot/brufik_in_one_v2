"""剧本任务引擎：剧本管理 + 任务实例状态机 + 分数流转 + LLM 工具函数。

设计契约（与后台模块编辑器一一对应）：
- 剧本 = JSON 定义文件（data/quest_playbooks/<name>.json），只存"定义"
  （goal/strategy/激活分数/成功失败条件/on_success/on_failure/pos）
- 实例 = 每设备每任务的运行态（DB quest_instance 表）
- 状态机：not_started --(current_score ≥ activation_score)--> running --(AI 判定)--> success/failed
- 分数流转：任务成功 → on_success 各目标加分；失败 → on_failure 各目标加分；
  目标当前分数 ≥ 激活分数 时自动激活（not_started → running，写入 started_at）
- 起点任务：定义里 initial_status=running 的任务在分配剧本时直接进入 running
- 工具函数（供 LLM tool loop 与后台模拟调用）：
  update_task_result(device_id, playbook, task_id, status, result)  置成功/失败并传播
  update_task_strategy(device_id, playbook, task_id, strategy)      AI 按用户反馈更新策略
- 终态（success/failed）不可再变；分数收入（contribute_score）只对未开始/进行中有效
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

from deskbot_server.dao import quest_mapper
from deskbot_server.utils.paths import DATA_DIR
from deskbot_server.utils.singleton import SingletonMeta

logger = logging.getLogger("deskbot-server")

# ── 任务状态常量 ──────────────────────────────────────────────
STATUS_NOT_STARTED = "not_started"
STATUS_RUNNING = "running"
STATUS_FAILED = "failed"
STATUS_SUCCESS = "success"
ALL_STATUS = (STATUS_NOT_STARTED, STATUS_RUNNING, STATUS_FAILED, STATUS_SUCCESS)
TERMINAL_STATUS = (STATUS_FAILED, STATUS_SUCCESS)

# 任务结果工具只接受 success/failed（对应剧本里的 on_success/on_failure 两条边）
RESULT_SUCCESS = "success"
RESULT_FAILED = "failed"
ALL_RESULTS = (RESULT_SUCCESS, RESULT_FAILED)

# 边端口名（成功口/失败口 → 定义里的 on_success/on_failure）
PORT_SUCCESS = "success"
PORT_FAILED = "failed"

# 单次分数收入上限（对话贡献分/时间收入的封顶，防单次调用直接打穿激活线）
MAX_SCORE_PER_CONTRIBUTE = 10

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


def _errs_for_task(task: dict, task_ids: set[str]) -> list[str]:
    errs: list[str] = []
    tid = str(task.get("id") or "")
    label = f"任务 {tid or '<无id>'}"
    if not tid or not TASK_ID_RE.match(tid):
        errs.append(f"{label}: id 非法（{tid!r}，需匹配 {TASK_ID_RE.pattern}）")
    if not str(task.get("goal") or "").strip():
        errs.append(f"{label}: goal 不能为空")
    act = task.get("activation_score")
    if act is None or not isinstance(act, (int, float)) or act < 0:
        errs.append(f"{label}: activation_score 必须是非负数字（{act!r}）")
    init = task.get("initial_status") or STATUS_NOT_STARTED
    if init not in (STATUS_NOT_STARTED, STATUS_RUNNING):
        errs.append(f"{label}: initial_status 只能是 not_started/running（{init!r}）")
    for port in ("on_success", "on_failure"):
        refs = task.get(port)
        if refs is None:
            continue
        if not isinstance(refs, list):
            errs.append(f"{label}: {port} 必须是列表")
            continue
        seen: set[str] = set()
        for ref in refs:
            if not isinstance(ref, dict) or not str(ref.get("id") or "").strip():
                errs.append(f"{label}: {port} 里的后继必须含 id")
                continue
            rid = str(ref["id"]).strip()
            if rid in seen:
                errs.append(f"{label}: {port} 后继 {rid} 重复")
            seen.add(rid)
            score = ref.get("score", 0)
            if not isinstance(score, (int, float)) or score < 0:
                errs.append(f"{label}: {port} 后继 {rid} 的 score 必须是非负数字")
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
        errs += _errs_for_task(t, ids)
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
        for port in ("on_success", "on_failure"):
            for ref in t.get(port) or []:
                rid = str(ref.get("id") or "") if isinstance(ref, dict) else ""
                if rid and rid not in ids:
                    errs.append(f"任务 {tid}: {port} 引用了不存在的任务 {rid}")
                if rid and tid and rid != tid:
                    edges.append((tid, rid))
    # 环检测（on_success + on_failure 合并构图）
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


def normalize_task(raw: dict, *, existing: list[dict] | None = None) -> dict:
    """补全任务默认字段（不校验，校验交给 validate_playbook）。"""
    existing = existing or []
    return {
        "id": str(raw.get("id") or "").strip(),
        "title": str(raw.get("title") or "notitle").strip(),
        "goal": str(raw.get("goal") or "").strip(),
        "strategy": str(raw.get("strategy") or "").strip(),
        "activation_score": int(raw.get("activation_score") or 1),
        "initial_status": str(raw.get("initial_status") or STATUS_NOT_STARTED).strip(),
        "success_condition": str(raw.get("success_condition") or "").strip(),
        "failure_condition": str(raw.get("failure_condition") or "").strip(),
        "on_success": _normalize_refs(raw.get("on_success")),
        "on_failure": _normalize_refs(raw.get("on_failure")),
        "score_sources": _normalize_score_sources(raw.get("score_sources")),
        "pos": _normalize_pos(raw.get("pos"), existing),
    }


def _normalize_refs(raw: Any) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for ref in raw:
        if isinstance(ref, dict) and str(ref.get("id") or "").strip():
            out.append({"id": str(ref["id"]).strip(), "score": int(ref.get("score") or 0)})
    return out


def _normalize_score_sources(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {"conversation": True, "time": None}
    time_val = raw.get("time")
    return {
        "conversation": bool(raw.get("conversation", True)),
        "time": str(time_val).strip() if time_val else None,
    }


def _normalize_pos(raw: Any, existing: list[dict]) -> dict:
    if not isinstance(raw, dict):
        return _default_pos(existing)
    base = _default_pos(existing)
    for key in ("x", "y", "width", "height"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            base[key] = int(val)
    return base


# ── 剧本任务引擎 ──────────────────────────────────────────────


class QuestService(metaclass=SingletonMeta):
    """剧本文件管理 + 任务实例状态机 + 分数流转 + 工具函数（无状态，状态全在文件/DB）。"""

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
        path = self._playbook_path(name)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QuestError(f"剧本 {name} 读取失败: {exc}") from exc

    def create_playbook(self, name: str) -> dict:
        if not PLAYBOOK_NAME_RE.match(name):
            raise QuestError(f"非法剧本名: {name!r}（需匹配 {PLAYBOOK_NAME_RE.pattern}）")
        if self.get_playbook(name) is not None:
            raise QuestError(f"剧本已存在: {name}")
        data = {"name": name, "tasks": []}
        self._write_playbook(name, data)
        return data

    def save_playbook(self, name: str, data: dict) -> dict:
        """整体保存（导入用）：校验通过后原子写盘。"""
        data = dict(data or {})
        data["name"] = name
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
        """更新任务字段；支持改名（id 变了会同步其他任务的连线和运行实例）。"""
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
            if key in ("pos",):
                merged[key] = _normalize_pos(val, tasks)
            elif key in ("on_success", "on_failure"):
                merged[key] = _normalize_refs(val)
            else:
                merged[key] = val
        merged = normalize_task(merged, existing=tasks)
        merged["id"] = new_id
        tasks[tasks.index(target)] = merged
        if renamed:
            # 其他任务的连线里指向旧 id 的引用同步改名，并去重
            for t in tasks:
                for port in ("on_success", "on_failure"):
                    seen: set[str] = set()
                    out: list[dict] = []
                    for ref in t.get(port) or []:
                        if ref["id"] == task_id:
                            ref["id"] = new_id
                        if ref["id"] not in seen:
                            seen.add(ref["id"])
                            out.append(ref)
                    t[port] = out
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
        # 清理其他任务的边里指向被删任务的引用
        for t in pb["tasks"]:
            for port in ("on_success", "on_failure"):
                t[port] = [r for r in t.get(port) or [] if r["id"] != task_id]
        self.save_playbook(name, pb)
        self._delete_task_instances(name, task_id)

    def set_edge(self, name: str, from_id: str, port: str, to_id: str, score: int) -> dict:
        """连边：from 的成功口/失败口 → to 的输入口，带分数。

        port ∈ {success, failed}；同一端口到同一目标只保留一条边（重复连 = 改分）。
        """
        pb = self._require_playbook(name)
        if port not in (PORT_SUCCESS, PORT_FAILED):
            raise QuestError(f"端口必须是 {PORT_SUCCESS}/{PORT_FAILED}（{port!r}）")
        tasks = pb.get("tasks") or []
        src = next((t for t in tasks if t["id"] == from_id), None)
        if src is None:
            raise QuestError(f"任务不存在: {from_id}")
        if not any(t["id"] == to_id for t in tasks):
            raise QuestError(f"任务不存在: {to_id}")
        if not isinstance(score, (int, float)) or score < 0:
            raise QuestError("分数必须是非负数字")
        key = "on_success" if port == PORT_SUCCESS else "on_failure"
        refs = src.get(key) or []
        refs = [r for r in refs if r["id"] != to_id]
        refs.append({"id": to_id, "score": int(score)})
        src[key] = refs
        self.save_playbook(name, pb)
        return {"from": from_id, "port": port, "to": to_id, "score": int(score)}

    def remove_edge(self, name: str, from_id: str, port: str, to_id: str) -> None:
        pb = self._require_playbook(name)
        if port not in (PORT_SUCCESS, PORT_FAILED):
            raise QuestError(f"端口必须是 {PORT_SUCCESS}/{PORT_FAILED}（{port!r}）")
        tasks = pb.get("tasks") or []
        src = next((t for t in tasks if t["id"] == from_id), None)
        if src is None:
            raise QuestError(f"任务不存在: {from_id}")
        key = "on_success" if port == PORT_SUCCESS else "on_failure"
        src[key] = [r for r in src.get(key) or [] if r["id"] != to_id]
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

    def ensure_instances(self, device_id: str, playbook_name: str) -> dict:
        """为设备创建剧本下缺失的任务实例（幂等）。

        定义里 initial_status=running 的任务（剧情起点）直接进入 running。
        """
        pb = self._require_playbook(playbook_name)
        existing = {r.task_id for r in quest_mapper.list_instances(device_id, playbook_name)}
        now = _utcnow_iso()
        created = activated = 0
        for t in pb.get("tasks") or []:
            tid = t["id"]
            if tid in existing:
                continue
            status = STATUS_RUNNING if t.get("initial_status") == STATUS_RUNNING else STATUS_NOT_STARTED
            quest_mapper.insert_instance(
                id=_new_id(),
                device_id=device_id,
                playbook=playbook_name,
                task_id=tid,
                status=status,
                current_score=0,
                started_at=now if status == STATUS_RUNNING else None,
                finished_at=None,
                result=None,
                strategy_override=None,
                created_at=now,
                updated_at=now,
            )
            created += 1
            if status == STATUS_RUNNING:
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

    def get_effective_strategy(self, device_id: str, playbook_name: str, task_id: str) -> str:
        """生效策略 = 实例级覆盖（update_task_strategy 写入）优先，否则取定义 strategy。"""
        row = quest_mapper.get_instance(device_id, playbook_name, task_id)
        if row is not None and row.strategy_override:
            return row.strategy_override
        defn = self.get_task_definition(playbook_name, task_id)
        return str((defn or {}).get("strategy") or "").strip()

    # ── 设备视角查询（运行时接线用）────────────────────────────

    def get_current_tasks(self, device_id: str) -> list[dict]:
        """设备当前进行中（running）的任务 —— 活跃目标集，供对话上下文注入。

        遍历设备名下所有剧本，返回运行中任务的目标/生效策略/成功失败条件/分数，
        按达成率（current_score/activation_score）降序（越接近完成越优先）。
        """
        out: list[dict] = []
        for playbook in self.list_playbooks():
            for inst in quest_mapper.list_instances(device_id, playbook):
                if inst.status != STATUS_RUNNING:
                    continue
                defn = self.get_task_definition(playbook, inst.task_id)
                if defn is None:
                    continue
                activation = max(int(defn.get("activation_score") or 1), 1)
                ratio = (inst.current_score or 0) / activation
                out.append(
                    {
                        "playbook": playbook,
                        "task_id": inst.task_id,
                        "title": defn.get("title", "notitle"),
                        "goal": defn.get("goal", ""),
                        "strategy": self.get_effective_strategy(device_id, playbook, inst.task_id),
                        "success_condition": defn.get("success_condition", ""),
                        "failure_condition": defn.get("failure_condition", ""),
                        "current_score": inst.current_score,
                        "activation_score": activation,
                        "ratio": round(ratio, 3),
                    }
                )
        out.sort(key=lambda x: x["ratio"], reverse=True)
        return out

    def get_tool_calls(self, device_id: str) -> list[dict]:
        """设备当前可用的剧情工具调用契约（供 LLM system prompt / tool loop 注册）。

        返回工具名/描述/参数说明，并附当前进行中任务 id（工具可操作的目标）。
        """
        task_ids = [t["task_id"] for t in self.get_current_tasks(device_id)]
        return [
            {
                "name": "update_task_result",
                "description": (
                    "判断任务的 success_condition 满足则置 success、failure_condition 满足则置 failed；"
                    "置终态后沿成功/失败连线向后继任务传播分数，后继达标会自动激活。result 必填"
                ),
                "parameters": {
                    "task_id": "string（目标任务 id）",
                    "status": 'string（"success" 或 "failed"）',
                    "result": "string（成功结果/失败原因）",
                },
                "available_task_ids": task_ids,
            },
            {
                "name": "update_task_strategy",
                "description": "根据用户反馈更新某任务的处理策略（实例级覆盖定义 strategy，后续对话按新策略执行）",
                "parameters": {
                    "task_id": "string（目标任务 id）",
                    "strategy": "string（新的处理策略）",
                },
                "available_task_ids": task_ids,
            },
            {
                "name": "contribute_score",
                "description": "对话对某任务有实质推进时加分（单次 0-10），当前分数达到激活分数后任务自动激活",
                "parameters": {
                    "task_id": "string（目标任务 id）",
                    "points": "number（0-10）",
                },
                "available_task_ids": task_ids,
            },
        ]

    # ── 分数收入（对话贡献分 / 时间收入共用入口）──────────────

    def contribute_score(self, device_id: str, playbook_name: str, task_id: str, points: int) -> dict:
        """给任务加当前分数，达到激活分数自动激活（not_started → running）。

        单次贡献封顶 MAX_SCORE_PER_CONTRIBUTE（防工具调用打穿激活线）。
        终态任务不接受分数。
        """
        points = int(points or 0)
        if points < 0:
            raise QuestError("分数必须非负")
        points = min(points, MAX_SCORE_PER_CONTRIBUTE)
        if points == 0:
            return self._instance_result(device_id, playbook_name, task_id, activated=False)
        row = self._require_instance(device_id, playbook_name, task_id)
        if row.status in TERMINAL_STATUS:
            raise QuestError(f"任务已是终态（{row.status}），不再接受分数")
        defn = self.get_task_definition(playbook_name, task_id)
        activation = int((defn or {}).get("activation_score") or 1)
        new_score = (row.current_score or 0) + points
        activated = row.status == STATUS_NOT_STARTED and new_score >= activation
        status = STATUS_RUNNING if activated else row.status
        now = _utcnow_iso()
        quest_mapper.update_instance(
            id=row.id,
            status=status,
            current_score=new_score,
            started_at=now if activated else _dt_str(row.started_at),
            finished_at=_dt_str(row.finished_at),
            result=row.result,
            strategy_override=row.strategy_override,
            updated_at=now,
        )
        return self._instance_result(device_id, playbook_name, task_id, activated=activated)

    # ── 工具函数（LLM tool loop 与后台模拟共用）────────────────

    def update_task_result(
        self, device_id: str, playbook_name: str, task_id: str, status: str, result: str
    ) -> dict:
        """工具函数①：AI 判断 success/failure 条件满足后调用。

        - status ∈ {success, failed}，result 为成功结果/失败原因（必填）
        - 任务须处于 running（未激活/已终态会抛错）
        - 置终态后沿 on_success/on_failure 向后继传播分数；
          后继当前分数 ≥ 激活分数 时自动激活（写入 started_at）
        - 返回 {"task": ..., "propagated": [...], "activated": [...]}
        """
        if status not in ALL_RESULTS:
            raise QuestError(f"status 必须是 {RESULT_SUCCESS}/{RESULT_FAILED}（{status!r}）")
        result = str(result or "").strip()
        if not result:
            raise QuestError("缺少结果描述（成功结果/失败原因）")
        row = self._require_instance(device_id, playbook_name, task_id)
        if row.status == STATUS_NOT_STARTED:
            raise QuestError(f"任务未激活（{task_id}），还不能判定结果")
        if row.status in TERMINAL_STATUS:
            raise QuestError(f"任务已是终态（{row.status}）")

        now = _utcnow_iso()
        quest_mapper.update_instance(
            id=row.id,
            status=status,
            current_score=row.current_score,
            started_at=_dt_str(row.started_at),
            finished_at=now,
            result=result,
            strategy_override=row.strategy_override,
            updated_at=now,
        )
        propagated, activated = self._propagate(
            device_id, playbook_name, task_id, status, now=now
        )
        return {
            "task": self._instance_result(device_id, playbook_name, task_id),
            "propagated": propagated,
            "activated": activated,
        }

    def set_state(
        self, device_id: str, playbook_name: str, task_id: str, status: str, result: str | None = None
    ) -> dict:
        """设计沙箱直接改实例状态（跨过工具契约校验，允许任意跳转，不传播分数）。

        终态写入 finished_at/result；改回 running 时补 started_at。
        """
        if status not in ALL_STATUS:
            raise QuestError(f"status 必须是 {ALL_STATUS}（{status!r}）")
        row = self._require_instance(device_id, playbook_name, task_id)
        now = _utcnow_iso()
        started_at = _dt_str(row.started_at)
        if status == STATUS_RUNNING and row.status != STATUS_RUNNING and not started_at:
            started_at = now
        finished_at = now if status in TERMINAL_STATUS else None
        quest_mapper.update_instance(
            id=row.id,
            status=status,
            current_score=row.current_score,
            started_at=started_at,
            finished_at=finished_at,
            result=str(result or "").strip() or None,
            strategy_override=row.strategy_override,
            updated_at=now,
        )
        return self._instance_result(device_id, playbook_name, task_id)

    def update_task_strategy(self, device_id: str, playbook_name: str, task_id: str, strategy: str) -> dict:
        """工具函数②：AI 根据用户反馈更新任务的处理策略（实例级覆盖定义 strategy）。"""
        strategy = str(strategy or "").strip()
        if not strategy:
            raise QuestError("strategy 不能为空")
        row = self._require_instance(device_id, playbook_name, task_id)
        now = _utcnow_iso()
        quest_mapper.update_instance(
            id=row.id,
            status=row.status,
            current_score=row.current_score,
            started_at=_dt_str(row.started_at),
            finished_at=_dt_str(row.finished_at),
            result=row.result,
            strategy_override=strategy,
            updated_at=now,
        )
        return {"task_id": task_id, "strategy": strategy}

    # ── 内部 ──────────────────────────────────────────────────

    def _propagate(self, device_id: str, playbook_name: str, task_id: str, status: str, *, now: str) -> tuple[list[dict], list[dict]]:
        defn = self.get_task_definition(playbook_name, task_id)
        port = "on_success" if status == RESULT_SUCCESS else "on_failure"
        refs = (defn or {}).get(port) or []
        propagated: list[dict] = []
        activated: list[dict] = []
        for ref in refs:
            target_id = ref["id"]
            tgt = quest_mapper.get_instance(device_id, playbook_name, target_id)
            if tgt is None:
                continue  # 定义引用了尚未分配实例的任务（改剧本后未重新分配）
            if tgt.status in TERMINAL_STATUS:
                continue  # 终态目标不再接收
            tgt_defn = self.get_task_definition(playbook_name, target_id)
            activation = int((tgt_defn or {}).get("activation_score") or 1)
            new_score = (tgt.current_score or 0) + int(ref.get("score") or 0)
            is_activated = tgt.status == STATUS_NOT_STARTED and new_score >= activation
            quest_mapper.update_instance(
                id=tgt.id,
                status=STATUS_RUNNING if is_activated else tgt.status,
                current_score=new_score,
                started_at=now if is_activated else _dt_str(tgt.started_at),
                finished_at=_dt_str(tgt.finished_at),
                result=tgt.result,
                strategy_override=tgt.strategy_override,
                updated_at=now,
            )
            info = {
                "task_id": target_id,
                "current_score": new_score,
                "status": STATUS_RUNNING if is_activated else tgt.status,
            }
            propagated.append(info)
            if is_activated:
                activated.append(info)
        return propagated, activated

    def _require_instance(self, device_id: str, playbook_name: str, task_id: str):
        row = quest_mapper.get_instance(device_id, playbook_name, task_id)
        if row is None:
            raise QuestError(f"任务实例不存在（剧本 {playbook_name}/{task_id}）——请先创建/分配实例")
        return row

    def _instance_result(self, device_id: str, playbook_name: str, task_id: str, *, activated: bool = False) -> dict:
        row = quest_mapper.get_instance(device_id, playbook_name, task_id)
        out = _instance_to_dict(row) if row else {"task_id": task_id}
        out["activated"] = activated
        return out


def _instance_to_dict(row) -> dict[str, Any]:
    return {
        "device_id": row.device_id,
        "playbook": row.playbook,
        "task_id": row.task_id,
        "status": row.status,
        "current_score": row.current_score,
        "started_at": _dt_str(row.started_at),
        "finished_at": _dt_str(row.finished_at),
        "result": row.result,
        "strategy_override": row.strategy_override,
    }


def _dt_str(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat(timespec="seconds")
    return str(val)
