"""Conservative incremental extraction; complex revisions use sourced Agent tools.

Only direct first-person declarations are automatically extracted. Questions,
quoted speech and hypothetical statements remain searchable daily evidence.
"""

from __future__ import annotations

import hashlib
import re

from deskbot_server.dao import memory_file_store as store
from deskbot_server.model.memory import MemoryVersionConflict
from deskbot_server.service import memory_service as memory


def candidates(text: str) -> list[tuple[str, str]]:
    if any(word in text for word in ("如果", "假如", "要是", "比如", "开玩笑", "他说", "她说", "别人说", "你说", "是不是", "？", "?", "“", '"')):
        return []
    result = []
    first_person = False
    for clause in re.split(r"[，,。！!；;\n]", text):
        clause = clause.strip(" ，,。")
        if not clause or len(clause) > 500:
            continue
        if first_person and re.match(r"(?:喜欢|不喜欢|爱好|习惯|正在)", clause):
            clause = "我" + clause
        first_person = bool(re.match(r"我(?:叫|的名字是|喜欢|不喜欢|的爱好是|爱好|习惯|正在)", clause))
        if re.search(r"我(?:叫|的名字是|住在|的职业是|从事)", clause):
            section = "基本信息"
        elif re.search(r"我(?:的爱好是|爱好|喜欢|不喜欢|讨厌)", clause):
            section = "最近关注" if any(w in clause for w in ("今天", "这次", "暂时", "最近")) else "爱好"
        elif re.search(r"我(?:希望|更喜欢|习惯)|请(?:以后|每次|始终)", clause):
            section = "偏好"
        elif re.search(r"我(?:正在|最近在|计划|准备)", clause):
            section = "项目" if any(w in clause for w in ("项目", "比赛", "论文", "开发")) else "最近关注"
        elif re.search(r"我(?:今天|最近|现在)?(?:心情|很难过|很开心|很焦虑|有点烦)", clause):
            section = "情绪"
        elif re.search(r"我(?:今天|最近|现在)?(?:头疼|头痛|发烧|感冒|不舒服|睡不好)", clause):
            section = "健康"
        else:
            continue
        result.append((section, "用户自述：" + clause))
    return result


def _fact_key(section: str, text: str) -> str | None:
    if section == "基本信息":
        for token, key in (("住在", "residence"), ("叫", "name"), ("名字是", "name"), ("职业是", "occupation")):
            if token in text:
                return key
    if section in {"爱好", "偏好"}:
        matched = re.search(r"(?:不喜欢|更喜欢|喜欢|讨厌|爱好是|爱好)(.+)$", text)
        return "interest:" + matched[1].strip(" ，,。") if matched else None
    if section == "情绪":
        return "current_mood"
    return None


def _apply_candidate(device_id, event, section, text, expected_epoch):
    key = "auto:" + event["id"] + ":" + hashlib.sha256((section + text).encode()).hexdigest()
    with store.transaction(device_id) as files:
        state = memory._state(files, expected_epoch)
        if hashlib.sha256(key.encode()).hexdigest() in state["requests"]:
            return True
    for _ in range(3):
        profile = memory.read_person(device_id, event["person_id"])
        fact_key = _fact_key(section, text)
        prior = next((e for e in reversed(profile["entries"]) if fact_key and e["section"] == section
                      and _fact_key(section, e["text"]) == fact_key), None)
        if prior and prior.get("observed_at", prior["updated_at"]) > event["occurred_at"]:
            return True
        try:
            memory.update_person(device_id, event["person_id"], section=section, text=text, source_ids=[event["id"]],
                                 entry_id=prior["id"] if prior and prior["text"] != text else None,
                                 expected_version=profile["version"], request_key=key, expected_epoch=expected_epoch)
            return True
        except MemoryVersionConflict:
            continue
    return False


def consolidate_event(device_id: str, event_id: str, *, expected_epoch: str | None = None) -> None:
    with store.transaction(device_id) as files:
        state = memory._state(files, expected_epoch)
        if event_id in state.get("processed_events", []) or event_id in state["erased"]:
            return
        event = memory._find_event(files, event_id)
        if not event or event["status"] == "erased" or event["kind"] != "user" or not event.get("person_id"):
            return
    # Each update has its own durable idempotency key. A crash between entries
    # can replay the source safely; an erase changes epoch and invalidates it.
    for section, text in candidates(event["text"]):
        if not _apply_candidate(device_id, event, section, text, expected_epoch):
            # Leave the event uncheckpointed; a future recovery can retry it.
            return
    with store.transaction(device_id) as files:
        state = memory._state(files, expected_epoch)
        if event_id not in state["erased"]:
            state.setdefault("processed_events", []).append(event_id)
            profile = memory._profile(files, state, event["person_id"])
            profile["processed_event"] = event_id
            memory._save_profile(files, state, profile)
            store.save_state(files, state)


def resume_consolidation(device_id: str, *, expected_epoch: str) -> None:
    with store.transaction(device_id) as files:
        state = memory._state(files, expected_epoch)
        completed = set(state.get("processed_events", [])) | set(state["erased"])
        pending = [e["id"] for _, _, e in memory._chat_events(files) if e["kind"] == "user"
                   and e.get("person_id") and e["id"] not in completed and e["status"] != "erased"]
    for event_id in pending:
        consolidate_event(device_id, event_id, expected_epoch=expected_epoch)
