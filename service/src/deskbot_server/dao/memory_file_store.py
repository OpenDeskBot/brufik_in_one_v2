"""Serialized, recoverable file operations for one device's memory directory.

The journal is filesystem transaction metadata, not another memory source.
Recovery finishes an interrupted transaction before any reader sees its files.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

from deskbot_server.service.device_data_paths import device_data_dir

_LOCKS = [threading.RLock() for _ in range(64)]
_BASENAME = re.compile(r"(?:chat_\d{8}\.txt|person_[^/\\\x00-\x1f]+\.txt|_memory_state\.json)\Z")
_JOURNAL = ".pending.json"


def memory_dir(device_id: str) -> Path:
    root = device_data_dir(device_id) / "memory"
    if root.is_symlink():
        raise ValueError("记忆目录不能是符号链接")
    return root


def _path(root: Path, name: str) -> Path:
    if not _BASENAME.fullmatch(name) and name != _JOURNAL:
        raise ValueError("记忆文件名无效")
    path = root / name
    if path.is_symlink():
        raise ValueError("记忆文件不能是符号链接")
    return path


def _sync_directory(root: Path) -> None:
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, text: str) -> None:
    descriptor, temp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _sync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


class MemoryFiles:
    def __init__(self, root: Path):
        self.root = root
        self.changes: dict[str, str | None] = {}
        self.appends: dict[str, str] = {}

    def read(self, name: str) -> str:
        if name in self.changes:
            return self.changes[name] or ""
        path = _path(self.root, name)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return text + self.appends.get(name, "")

    def names(self, pattern: str) -> list[str]:
        names = ({p.name for p in self.root.glob(pattern)}
                 | {n for n in self.changes if Path(n).match(pattern)}
                 | {n for n in self.appends if Path(n).match(pattern)})
        return sorted(n for n in names if self.changes.get(n, "exists") is not None)

    def write(self, name: str, text: str) -> None:
        _path(self.root, name)
        if self.read(name) == text:
            return
        self.changes[name] = text
        self.appends.pop(name, None)

    def append(self, name: str, text: str) -> None:
        _path(self.root, name)
        if name in self.changes:
            self.changes[name] = (self.changes[name] or "") + text
        else:
            self.appends[name] = self.appends.get(name, "") + text

    def delete(self, name: str) -> None:
        _path(self.root, name)
        self.changes[name] = None
        self.appends.pop(name, None)

    def commit(self) -> None:
        if not self.changes and not self.appends:
            return
        operations = [{"name": name, "text": text, "append": False} for name, text in self.changes.items()]
        for name, text in self.appends.items():
            path = _path(self.root, name)
            operations.append({"name": name, "text": text, "append": True,
                               "offset": path.stat().st_size if path.exists() else 0})
        _atomic_write(self.root / _JOURNAL, json.dumps(operations, ensure_ascii=False))
        _apply(self.root, operations)
        (self.root / _JOURNAL).unlink()
        _sync_directory(self.root)
        self.changes.clear()
        self.appends.clear()


def _apply(root: Path, operations: list[dict]) -> None:
    for operation in operations:
        path = _path(root, operation["name"])
        content = operation["text"]
        if content is None:
            path.unlink(missing_ok=True)
        elif operation["append"]:
            # A crash may leave part/all of an append. Replaying truncates only
            # this transaction's suffix, then writes the exact intended bytes.
            with path.open("r+b" if path.exists() else "w+b") as stream:
                if stream.seek(0, os.SEEK_END) < operation["offset"]:
                    raise OSError("记忆文件比恢复位置更短，停止恢复")
                stream.truncate(operation["offset"])
                stream.seek(operation["offset"])
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        else:
            _atomic_write(path, content)
    _sync_directory(root)


@contextlib.contextmanager
def device_guard(device_id: str):
    root = memory_dir(device_id)
    stripe = int(hashlib.sha256(str(root).encode()).hexdigest()[:8], 16) % len(_LOCKS)
    with _LOCKS[stripe]:
        yield root


@contextlib.contextmanager
def transaction(device_id: str) -> Iterator[MemoryFiles]:
    with device_guard(device_id) as root:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = root / ".lock"
        if lock_path.is_symlink():
            raise ValueError("记忆锁文件不能是符号链接")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                journal = _path(root, _JOURNAL)
                if journal.exists():
                    _apply(root, json.loads(journal.read_text(encoding="utf-8")))
                    journal.unlink()
                    _sync_directory(root)
                for leftover in root.glob(".write-*"):
                    leftover.unlink()
                files = MemoryFiles(root)
                yield files
                files.commit()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def load_state(files: MemoryFiles) -> dict:
    text = files.read("_memory_state.json")
    if text:
        state = json.loads(text)
        if state.get("schema") != 1:
            raise ValueError("不支持的记忆文件版本")
        return state
    if files.names("person_*.txt"):
        raise ValueError("人物索引丢失，请恢复记忆目录备份")
    return {"schema": 1, "epoch": uuid.uuid4().hex, "revision": 0, "people": {}, "identities": {},
            "requests": {}, "erased": [], "legacy_imported": False}


def save_state(files: MemoryFiles, state: dict) -> None:
    files.write("_memory_state.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")
