"""Daily conversation evidence and sourced person profiles in device files."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta

from deskbot_server.dao import memory_file_store as store
from deskbot_server.model.memory import (
    MEMORY_TZ,
    SECTIONS,
    TRANSIENT_DAYS,
    MemoryEpochChanged,
    MemoryVersionConflict,
    clean_text,
    decode_line,
    encode_line,
    now_iso,
)

logger = logging.getLogger("deskbot-server")
_EVENT_ID = re.compile(r"\d{8}-[a-f0-9]{24}\Z")
_LISTENERS = {}
_LISTENER_LOCK = threading.Lock()


def subscribe(device_id, callback):
    key = str(store.memory_dir(device_id))
    with _LISTENER_LOCK:
        _LISTENERS.setdefault(key, set()).add(callback)

    def unsubscribe():
        with _LISTENER_LOCK:
            callbacks = _LISTENERS.get(key, set())
            callbacks.discard(callback)
            if not callbacks:
                _LISTENERS.pop(key, None)
    return unsubscribe


def _notify(device_id, *, reset=False):
    with _LISTENER_LOCK:
        callbacks = tuple(_LISTENERS.get(str(store.memory_dir(device_id)), ()))
    for callback in callbacks:
        try:
            callback(reset=reset)
        except Exception:
            logger.warning("[memory] 会话通知失败 device_id=%s", device_id)


def _state(files, expected_epoch=None):
    state = store.load_state(files)
    if expected_epoch is not None and state["epoch"] != expected_epoch:
        raise MemoryEpochChanged("记忆已清除或会话已失效，请重新连接")
    return state


def memory_epoch(device_id):
    with store.transaction(device_id) as files:
        state = _state(files)
        store.save_state(files, state)
        return state["epoch"]


def link_turn_sources(device_id, turn_id, source_ids, *, expected_epoch=None):
    """Persist evidence read by a turn so later recalls can be erased with it."""
    if not turn_id or not source_ids:
        return
    with store.transaction(device_id) as files:
        state = _state(files, expected_epoch)
        if turn_id in state.get("erased_turns", []) or set(source_ids).intersection(state["erased"]):
            raise MemoryEpochChanged("本轮引用的记忆已遗忘，请重新连接")
        links = state.setdefault("turn_sources", {})
        links[turn_id] = sorted(set(links.get(turn_id, [])) | set(source_ids))
        store.save_state(files, state)


def _profile(files, state, person_id):
    if not isinstance(person_id, str):
        raise ValueError("人物编号无效")
    person = state["people"].get(person_id)
    if person is None:
        raise ValueError("人物不存在于当前设备")
    lines = files.read(person["file"]).splitlines()
    header = decode_line(lines[0]) if lines else None
    if not header or header[1].get("id") != person_id:
        raise ValueError("人物文件缺失或标识不一致，请恢复备份")
    return {**header[1], "entries": [value[1] for line in lines[1:] if (value := decode_line(line))]}


def _save_profile(files, state, profile):
    document = encode_line("人物资料", {k: v for k, v in profile.items() if k != "entries"})
    for section in SECTIONS:
        document += f"\n[{section}]\n"
        for entry in profile["entries"]:
            if entry["section"] == section:
                suffix = "（已被纠正）" if entry["status"] == "superseded" else ""
                document += encode_line("- " + entry["text"] + suffix, entry)
    files.write(state["people"][profile["id"]]["file"], document)


def _ensure_person(files, state, name, *, identity=None, person_id=None, rename=False):
    name = clean_text(name, maximum=80)
    if any(ord(c) < 32 for c in name):
        raise ValueError("姓名不能包含控制字符")
    existing_identity = state["identities"].get(identity) if identity else None
    if existing_identity and person_id and person_id != existing_identity:
        raise ValueError("识别档案已关联其他人物，请核对人物编号")
    person_id = existing_identity or person_id
    if person_id:
        profile = _profile(files, state, person_id)
    else:
        candidates = [pid for pid, p in state["people"].items() if name in p["aliases"]]
        if identity:
            modality = identity.split(":", 1)[0] + ":"
            candidates = [pid for pid in candidates if not any(k.startswith(modality) and p == pid for k, p in state["identities"].items())]
        if len(candidates) > 1:
            raise ValueError("有同名人物，请指定人物编号")
        if candidates:
            profile = _profile(files, state, candidates[0])
        else:
            pid = "p" + uuid.uuid4().hex[:24]
            safe = re.sub(r'[\\/<>:"|?*\x00-\x1f]', "_", name).strip(". ") or "未命名"
            while len(safe.encode("utf-8")) > 160:
                safe = safe[:-1]
            filename = f"person_{safe}.txt"
            if any(p["file"] == filename for p in state["people"].values()):
                filename = f"person_{safe}_{pid[1:9]}.txt"
            state["people"][pid] = {"name": name, "aliases": [name], "file": filename}
            profile = {"id": pid, "name": name, "aliases": [name], "version": 0,
                       "updated_at": now_iso(), "processed_event": None, "entries": []}
    if name not in profile["aliases"]:
        profile["aliases"].append(name)
    if rename:
        profile["name"] = name
        safe = re.sub(r'[\\/<>:"|?*\x00-\x1f]', "_", name).strip(". ") or "未命名"
        while len(safe.encode("utf-8")) > 160:
            safe = safe[:-1]
        filename = f"person_{safe}.txt"
        if any(p["file"] == filename and pid != profile["id"] for pid, p in state["people"].items()):
            filename = f"person_{safe}_{profile['id'][1:9]}.txt"
        old_file = state["people"][profile["id"]]["file"]
        if filename != old_file:
            files.delete(old_file)
            state["people"][profile["id"]]["file"] = filename
    state["people"][profile["id"]].update(name=profile["name"], aliases=profile["aliases"])
    if identity:
        state["identities"][identity] = profile["id"]
    _save_profile(files, state, profile)
    return profile


def ensure_person(device_id, name, *, identity=None, person_id=None, expected_epoch=None, rename=False):
    with store.transaction(device_id) as files:
        state = _state(files, expected_epoch)
        profile = _ensure_person(files, state, name, identity=identity, person_id=person_id, rename=rename)
        store.save_state(files, state)
        return {k: v for k, v in profile.items() if k != "entries"}


def list_people(device_id):
    with store.transaction(device_id) as files:
        state = _state(files)
        return [{"id": pid, **p, "version": _profile(files, state, pid)["version"]}
                for pid, p in state["people"].items()]


def read_person(device_id, person_id, *, section=None, include_history=False):
    if section is not None and section not in SECTIONS:
        raise ValueError("人物资料分块无效")
    with store.transaction(device_id) as files:
        profile = _profile(files, _state(files), person_id)
        profile["entries"] = [{**e, "expired": bool(e.get("expires_at") and e["expires_at"] <= now_iso())}
                              for e in profile["entries"] if (include_history or e["status"] == "active")
                              and (section is None or e["section"] == section)]
        return profile


def _chat_events(files, *, date_from=None, date_to=None):
    for day in (date_from, date_to):
        if day is not None and (not isinstance(day, str) or not re.fullmatch(r"\d{8}", day)):
            raise ValueError("日期需要 YYYYMMDD")
    for name in files.names("chat_*.txt"):
        day = name[5:13]
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        for line in files.read(name).splitlines():
            if value := decode_line(line):
                yield name, value[0], value[1]


def _find_event(files, event_id):
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise ValueError("事件编号无效")
    return next((e for _, _, e in _chat_events(files, date_from=event_id[:8], date_to=event_id[:8])
                 if e["id"] == event_id), None)


def _append_event(files, state, *, kind, text, actor, external_id, person_id=None, identity_basis="unknown",
                  turn_id=None, occurred_at=None, status="observed", source_ids=None):
    timestamp = datetime.fromisoformat(occurred_at) if occurred_at else datetime.now(MEMORY_TZ)
    if timestamp.tzinfo is None:
        raise ValueError("事件时间必须带时区")
    timestamp = timestamp.astimezone(MEMORY_TZ)
    day = timestamp.strftime("%Y%m%d")
    digest = hashlib.sha256(external_id.encode()).hexdigest()
    eid = state.setdefault("event_keys", {}).get(digest) or day + "-" + digest[:24]
    if (eid in state["erased"] or (turn_id and turn_id in state.get("erased_turns", []))
            or (kind != "user" and turn_id in state.get("erased_output_turns", []))):
        return {"id": eid, "status": "erased"}
    if existing := _find_event(files, eid):
        return existing
    event = {"id": eid, "kind": kind, "text": text, "actor": actor, "person_id": person_id,
             "identity_basis": identity_basis, "turn_id": turn_id, "occurred_at": timestamp.isoformat(),
             "received_at": now_iso(), "status": status,
             "source_ids": sorted(set(source_ids or []) | (set(state.get("turn_sources", {}).get(turn_id, [])) if kind != "user" else set()))}
    verb = "说：" if kind in {"user", "assistant"} else "操作："
    basis = f"〔{identity_basis}推测〕" if identity_basis in {"voice", "face"} else ""
    files.append(f"chat_{day}.txt", encode_line(timestamp.strftime("%H:%M:%S") + f" {actor}{basis} {verb}{text}〔{status}〕", event))
    state["event_keys"][digest] = eid
    return event


def record_chat_event(device_id, *, kind, text, external_id, person_id=None, identity_basis="unknown",
                      turn_id=None, occurred_at=None, status="observed", expected_epoch=None):
    if kind not in {"user", "assistant", "action", "playback"}:
        raise ValueError("记录类型无效")
    text = clean_text(text, maximum=32000)
    with store.transaction(device_id) as files:
        state = _state(files, expected_epoch)
        if person_id and person_id not in state["people"]:
            raise ValueError("人物不存在于当前设备")
        actor = state["people"][person_id]["name"] if kind == "user" and person_id else "未知说话人" if kind == "user" else "机器人"
        event = _append_event(files, state, kind=kind, text=text, actor=actor, external_id=external_id,
                              person_id=person_id, identity_basis=identity_basis, turn_id=turn_id,
                              occurred_at=occurred_at, status=status)
        store.save_state(files, state)
        return event


def attribute_self_introduction(device_id, event_id, person_id, *, expected_epoch=None):
    """Bind an enrollment's explicit same-turn name, preserving an audit record."""
    with store.transaction(device_id) as files:
        state = _state(files, expected_epoch)
        profile = _profile(files, state, person_id)
        event = _find_event(files, event_id)
        if not event or event["kind"] != "user" or event["status"] == "erased":
            return False
        if event.get("person_id"):
            return event["person_id"] == person_id
        if not re.search(r"我(?:叫|的名字是)\s*" + re.escape(profile["name"]) + r"(?=[，,。！!？?；;\s]|$)", event["text"]):
            return False
        filename = f"chat_{event_id[:8]}.txt"
        lines = []
        for name, visible, value in _chat_events(files, date_from=event_id[:8], date_to=event_id[:8]):
            if name != filename:
                continue
            if value["id"] == event_id:
                value = {**value, "person_id": person_id, "actor": profile["name"], "identity_basis": "self_declared"}
                visible = value["occurred_at"][11:19] + f" {profile['name']}〔用户自述〕说：{value['text']}〔observed〕"
            lines.append(encode_line(visible, value))
        files.write(filename, "".join(lines))
        _append_event(files, state, kind="identity_revision", text=f"根据本轮自我介绍补充发言者：{profile['name']}",
                      actor="机器人", external_id="attribution:" + event_id, person_id=person_id,
                      source_ids=[event_id], turn_id=event.get("turn_id"))
        store.save_state(files, state)
        return True


def search_chat(device_id, query="", *, person_id=None, date_from=None, date_to=None, limit=20, offset=0):
    if not isinstance(query, str) or len(query) > 200 or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError("搜索参数无效")
    terms = query.casefold().split()
    with store.transaction(device_id) as files:
        events = [e for _, _, e in _chat_events(files, date_from=date_from, date_to=date_to)
                  if e["status"] != "erased" and (not person_id or e.get("person_id") == person_id)
                  and all(t in e["text"].casefold() for t in terms)]
    events.sort(key=lambda e: (e["occurred_at"], e["id"]), reverse=True)
    return {"events": [{**e, "text": e["text"][:1000], "truncated": len(e["text"]) > 1000} for e in events[offset:offset + limit]],
            "total": len(events), "has_more": offset + limit < len(events), "offset": offset}


def read_chat(device_id, event_id, *, context=2):
    if type(context) is not int or not 0 <= context <= 10:
        raise ValueError("上下文条数需要 0～10")
    with store.transaction(device_id) as files:
        target = _find_event(files, event_id)
        if target is None or target["status"] == "erased":
            raise ValueError("记录不存在或已遗忘")
        events = [e for _, _, e in _chat_events(files, date_from=event_id[:8], date_to=event_id[:8]) if e["status"] != "erased"]
        index = next(i for i, e in enumerate(events) if e["id"] == event_id)
        return {"event": target, "context": events[max(0, index - context):index + context + 1]}


def _request(state, key, payload):
    if key is None:
        return None, None, None
    if not isinstance(key, str) or not 1 <= len(key) <= 256:
        raise ValueError("请求标识无效")
    digest = hashlib.sha256(key.encode()).hexdigest()
    content_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    previous = state["requests"].get(digest)
    if previous and previous["hash"] != content_hash:
        raise ValueError("请求标识已用于其他内容，请重新提交")
    return digest, content_hash, previous


def update_person(device_id, person_id, *, section, text, source_ids=None, entry_id=None, expected_version=None,
                  expires_at=None, request_key=None, manual=False, expected_epoch=None, additional_source_ids=None):
    text = clean_text(text)
    if section not in SECTIONS:
        raise ValueError("人物资料分块无效")
    if source_ids is not None and (not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 20
                                   or any(not isinstance(s, str) for s in source_ids)):
        raise ValueError("需要 1～20 个来源事件编号")
    if expected_version is not None and (type(expected_version) is not int or expected_version < 0):
        raise ValueError("资料版本无效")
    if entry_id is not None and not isinstance(entry_id, str):
        raise ValueError("条目编号无效")
    if expires_at is not None and not isinstance(expires_at, str):
        raise ValueError("失效时间无效")
    if expires_at:
        expiry = datetime.fromisoformat(expires_at)
        if expiry.tzinfo is None:
            raise ValueError("失效时间必须带时区")
        expires_at = expiry.astimezone(MEMORY_TZ).isoformat()
    payload = {"person_id": person_id, "section": section, "text": text, "entry_id": entry_id,
               "source_ids": source_ids, "expires_at": expires_at}
    with store.transaction(device_id) as files:
        state = _state(files, expected_epoch)
        profile = _profile(files, state, person_id)
        rid, content_hash, previous = _request(state, request_key, payload)
        if previous:
            found = next((e for e in profile["entries"] if e["id"] == previous["entry_id"]), None)
            return {"person_id": person_id, "entry": found, "version": profile["version"],
                    "status": "unchanged" if found else "erased", "saved": True}
        if expected_version is not None and profile["version"] != expected_version:
            raise MemoryVersionConflict(f"人物资料已变化，请重新读取（当前版本 {profile['version']}）")
        # Internal ASR fragment expansion must not change the caller's durable
        # request fingerprint when a tool result is retried after an answer.
        sources = list(dict.fromkeys([*(source_ids or []), *(additional_source_ids or [])]))
        if len(sources) > 20:
            raise ValueError("需要 1～20 个来源事件编号")
        if manual:
            audit = _append_event(files, state, kind="profile_revision", text=f"修订{profile['name']}的{section}：{text}",
                                  actor="管理者", person_id=person_id, identity_basis="manual",
                                  external_id="manual:" + (request_key or uuid.uuid4().hex))
            sources = [audit["id"]]
        elif not sources:
            raise ValueError("人物资料必须有本设备的用户发言来源")
        observed_at = ""
        for sid in sources:
            source = _find_event(files, sid)
            if not source or source["status"] == "erased" or sid in state["erased"]:
                raise ValueError("来源记录不存在或已遗忘")
            if source["status"] == "transcription_failed":
                raise ValueError("来源没有有效转写，不能据此整理事实")
            if source.get("person_id") != person_id or source["kind"] not in {"user", "profile_revision", "legacy_import"}:
                raise ValueError("来源必须是该人物的用户发言或人工修订，不能使用他人或机器人回复")
            observed_at = max(observed_at, source["occurred_at"])
        old = next((e for e in profile["entries"] if e["id"] == entry_id and e["status"] == "active"), None)
        if entry_id and old is None:
            raise ValueError("待纠正条目不存在，请重新读取")
        duplicate = next((e for e in profile["entries"] if e["status"] == "active" and e["section"] == section
                          and e["text"].casefold() == text.casefold()), None)
        if duplicate:
            merged_sources = list(dict.fromkeys([*duplicate["source_ids"], *sources, *(old["source_ids"] if old else [])]))
            changed = merged_sources != duplicate["source_ids"] or (old and old["id"] != duplicate["id"])
            if old and old["id"] != duplicate["id"]:
                old["status"] = "superseded"
                old["replaced_by"] = duplicate["id"]
            if changed:
                previous_observed = duplicate.get("observed_at", duplicate["updated_at"])
                duplicate["source_ids"] = merged_sources
                duplicate["updated_at"] = now_iso()
                if observed_at >= previous_observed:
                    duplicate["observed_at"] = observed_at
                    if expires_at is not None:
                        duplicate["expires_at"] = expires_at
                    elif section in TRANSIENT_DAYS:
                        duplicate["expires_at"] = (datetime.fromisoformat(observed_at) + timedelta(days=TRANSIENT_DAYS[section])).isoformat()
                profile["version"] += 1
                profile["updated_at"] = duplicate["updated_at"]
                state["revision"] += 1
            entry, status = duplicate, "updated" if old and old["id"] != duplicate["id"] else "unchanged"
        else:
            if old:
                old["status"] = "superseded"
            if expires_at is None and section in TRANSIENT_DAYS:
                expires_at = (datetime.fromisoformat(observed_at) + timedelta(days=TRANSIENT_DAYS[section])).isoformat()
            timestamp = now_iso()
            entry = {"id": "m" + uuid.uuid4().hex[:24], "person_id": person_id, "section": section, "text": text,
                     "source_ids": sources, "created_at": timestamp, "updated_at": timestamp,
                     "observed_at": observed_at,
                     "expires_at": expires_at, "status": "active", "replaces": entry_id}
            profile["entries"].append(entry)
            profile["version"] += 1
            profile["updated_at"] = timestamp
            state["revision"] += 1
            status = "updated" if old else "created"
        if rid:
            state["requests"][rid] = {"hash": content_hash, "entry_id": entry["id"]}
        _save_profile(files, state, profile)
        store.save_state(files, state)
        result = {"person_id": person_id, "entry": entry, "version": profile["version"], "status": status, "saved": True}
    _notify(device_id)
    return result


def forget_memory(device_id, *, entry_ids=None, event_ids=None, person_id=None, expected_epoch=None):
    if not entry_ids and not event_ids and not person_id:
        raise ValueError("需要指定要遗忘的条目、事件或人物")
    if any(not isinstance(v, list) or len(v) > 100 or any(not isinstance(i, str) for i in v) for v in (entry_ids or [], event_ids or [])):
        raise ValueError("遗忘编号列表无效")
    with store.transaction(device_id) as files:
        state = _state(files, expected_epoch)
        if person_id is not None and not isinstance(person_id, str):
            raise ValueError("人物编号无效")
        if person_id and person_id not in state["people"]:
            raise ValueError("人物不存在于当前设备")
        profiles = [_profile(files, state, pid) for pid in state["people"]]
        requested, targets = set(entry_ids or []), set(event_ids or [])
        for eid in targets:
            if not _find_event(files, eid) and eid not in state["erased"]:
                raise ValueError("记录不存在于当前设备")
        found = {e["id"] for p in profiles for e in p["entries"]}
        if requested - found - set(state.get("erased_entries", [])):
            raise ValueError("条目不存在于当前设备")
        for p in profiles:
            for e in p["entries"]:
                if e["id"] in requested or p["id"] == person_id:
                    targets.update(e["source_ids"])
                    requested.add(e["id"])
        events = list(_chat_events(files))
        turns = set(state.get("erased_turns", []))
        output_turns = set(state.get("erased_output_turns", []))
        changed = True
        while changed:
            before = (len(targets), len(requested), len(turns), len(output_turns))
            output_turns.update(turn for turn, sources in state.get("turn_sources", {}).items() if targets.intersection(sources))
            turns.update(e["turn_id"] for _, _, e in events if e["id"] in targets and e.get("turn_id")
                         and e["kind"] not in {"assistant", "action", "playback"})
            for _, _, e in events:
                if ((person_id and e.get("person_id") == person_id) or e.get("turn_id") in turns
                        or (e.get("turn_id") in output_turns and e["kind"] != "user")
                        or targets.intersection(e.get("source_ids", []))):
                    targets.add(e["id"])
            for p in profiles:
                for e in p["entries"]:
                    if e["id"] in requested or targets.intersection(e["source_ids"]) or e.get("replaces") in requested:
                        requested.add(e["id"])
                        if e.get("replaces"):
                            requested.add(e["replaces"])
                        targets.update(e["source_ids"])
            changed = (len(targets), len(requested), len(turns), len(output_turns)) != before
        rewritten = {}
        for filename, visible, e in events:
            if e["id"] in targets:
                e = {**e, "text": "", "actor": "已遗忘", "person_id": None, "identity_basis": "unknown",
                     "status": "erased", "source_ids": []}
                visible = e["occurred_at"][11:19] + " 已遗忘的记录"
            rewritten.setdefault(filename, []).append(encode_line(visible, e))
        for filename, lines in rewritten.items():
            files.write(filename, "".join(lines))
        for p in profiles:
            kept = [e for e in p["entries"] if e["id"] not in requested and not targets.intersection(e["source_ids"])]
            if len(kept) != len(p["entries"]):
                p["entries"], p["updated_at"] = kept, now_iso()
                p["version"] += 1
                _save_profile(files, state, p)
        if person_id:
            files.delete(state["people"][person_id]["file"])
            state["people"].pop(person_id)
            state["identities"] = {key: pid for key, pid in state["identities"].items() if pid != person_id}
        state["erased"] = sorted(set(state["erased"]) | targets)
        state["erased_entries"] = sorted(set(state.get("erased_entries", [])) | requested)
        state["erased_turns"] = sorted(turns)
        state["erased_output_turns"] = sorted(output_turns)
        state["turn_sources"] = {turn: sources for turn, sources in state.get("turn_sources", {}).items() if turn not in turns | output_turns}
        state["epoch"] = uuid.uuid4().hex
        state["revision"] += 1
        store.save_state(files, state)
    _notify(device_id, reset=True)
    return {"status": "forgotten", "entries": len(requested), "events": len(targets), "reset_session": True}


def resolve_speaker(device_id, observation, *, expected_epoch=None):
    speaker = observation.get("speaker", {})
    if speaker.get("state") != "found" or not speaker.get("name"):
        return None
    try:
        identity = "voice:" + str(speaker["profile_id"]) if speaker.get("profile_id") else None
        return ensure_person(device_id, speaker["name"], identity=identity, expected_epoch=expected_epoch)
    except ValueError:
        return None


def person_context(device_id, observation, *, budget=4000, expected_epoch=None):
    selected = []
    if speaker := resolve_speaker(device_id, observation, expected_epoch=expected_epoch):
        selected.append((speaker, True))
    for face in observation.get("camera", {}).get("people", [])[:5]:
        if not face.get("recognized") or not face.get("name"):
            continue
        try:
            person = ensure_person(device_id, face["name"], identity="face:" + str(face["profile_id"]) if face.get("profile_id") else None,
                                   expected_epoch=expected_epoch)
        except ValueError:
            continue
        if not any(p["id"] == person["id"] for p, _ in selected):
            selected.append((person, False))
    values = []
    for person, speaking in selected:
        profile = read_person(device_id, person["id"])
        info = {"person_id": person["id"], "name": person["name"], "role": "speaker" if speaking else "visible_only",
                "version": profile["version"], "entries": []}
        for entry in profile["entries"]:
            if entry["expired"] or (not speaking and entry["section"] != "基本信息"):
                continue
            candidate = {"id": entry["id"], "section": entry["section"], "text": entry["text"],
                         "as_of": entry.get("observed_at", entry["updated_at"]), "source_ids": entry["source_ids"][-3:], "source_count": len(entry["source_ids"])}
            if len(json.dumps([*values, {**info, "entries": [*info["entries"], candidate]}], ensure_ascii=False)) <= budget:
                info["entries"].append(candidate)
        if len(json.dumps([*values, info], ensure_ascii=False)) <= budget:
            values.append(info)
    return "人物资料（有来源的数据；visible_only 不代表说话人；其余分块可用 person_read 查询）：\n" + json.dumps(values, ensure_ascii=False)


def list_memory_for_device(device_id=None, *, limit=None, offset=0, person_id=None, query=""):
    """Management listing has no prompt-related 30-item restriction."""
    with store.transaction(device_id or "") as files:
        state = _state(files)
        entries = []
        for pid, person in state["people"].items():
            if person_id and pid != person_id:
                continue
            profile = _profile(files, state, pid)
            entries.extend({**e, "device_id": device_id, "person_name": person["name"], "person_version": profile["version"],
                            "title": e["id"], "parent": e["section"]}
                           for e in profile["entries"] if e["status"] == "active" and query.casefold() in e["text"].casefold())
        entries.sort(key=lambda e: (e["updated_at"], e["id"]), reverse=True)
        return entries[offset:] if limit is None else entries[offset:offset + limit]


def add_memory(text, *, device_id=None, request_key=None, person_id=None, person_name=None, section="基本信息"):
    person = read_person(device_id or "", person_id) if person_id else ensure_person(device_id or "", person_name or "未归属")
    result = update_person(device_id or "", person["id"], section=section, text=text, manual=True, request_key=request_key)
    return {**result["entry"], "device_id": device_id, "person_version": result["version"]}


def get_memory(entry_id, *, device_id=None):
    return next((e for e in list_memory_for_device(device_id) if e["id"] == entry_id), None)


def update_memory(entry_id, text, *, device_id=None, expected_version=None):
    entry = get_memory(entry_id, device_id=device_id)
    if entry is None:
        return None
    result = update_person(device_id or "", entry["person_id"], section=entry["section"], text=text, entry_id=entry_id,
                           expected_version=expected_version, manual=True)
    return {**result["entry"], "device_id": device_id, "person_version": result["version"]}


def delete_memory(entry_id, *, device_id=None):
    if get_memory(entry_id, device_id=device_id) is None:
        return False
    forget_memory(device_id or "", entry_ids=[entry_id])
    return True


def clear_file_memory(device_id):
    """Erase both kinds of file memory while invalidating in-flight writers."""
    with store.transaction(device_id) as files:
        state = _state(files)
        count = sum(len(_profile(files, state, pid)["entries"]) for pid in state["people"])
        for name in files.names("person_*.txt") + files.names("chat_*.txt"):
            files.delete(name)
        state = {"schema": 1, "epoch": uuid.uuid4().hex, "revision": state["revision"] + 1,
                 "people": {}, "identities": {}, "requests": {}, "erased": [], "legacy_imported": True}
        store.save_state(files, state)
    _notify(device_id, reset=True)
    return count
