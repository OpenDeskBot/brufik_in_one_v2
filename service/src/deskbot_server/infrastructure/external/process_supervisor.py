"""外部服务进程原语：spawn / 存活检测 / 终止 / pid 文件 / 日志重定向。

进程默认跟随主服务生命周期（同进程组，不 setsid）：主服务被 Ctrl+C 时
子进程同组收到 SIGINT；主服务异常退出（如 SIGKILL）时可能遗留孤儿进程，
由 manager 层通过 pid 文件检测并接管。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("deskbot-server")

TERM_GRACE_S = 5.0  # SIGTERM 后等待优雅退出的秒数，超时升级 SIGKILL


@dataclass
class SpawnConfig:
    command: list[str]
    workdir: Path
    log_path: Path
    pid_path: Path
    env: dict[str, str] = field(default_factory=dict)  # 附加环境变量（合并进 os.environ）

    def log_dir(self) -> Path:
        return self.log_path.parent


class ExternalProcess:
    """单个外部进程句柄。非线程安全，仅在 manager 的事件循环内使用。"""

    def __init__(self, config: SpawnConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self._env: dict[str, str] | None = None

    # -- 环境 --

    def env(self) -> dict[str, str]:
        if self._env is None:
            merged = dict(os.environ)
            merged.update(self.config.env)
            self._env = merged
        return self._env

    # -- 生命周期 --

    async def spawn(self) -> None:
        """启动子进程；stdout/stderr 合并重定向到日志文件，写入 pid 文件。

        注意：不能把 Python 文件对象传给 stdout（asyncio 要求非阻塞 fd，
        阻塞模式文件对象行为不可靠），须用 os.open 的 fd。
        """
        cfg = self.config
        cfg.log_dir().mkdir(parents=True, exist_ok=True)
        cfg.pid_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(str(cfg.log_path), os.O_CREAT | os.O_WRONLY | os.O_APPEND)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *cfg.command,
                cwd=str(cfg.workdir),
                env=self.env(),
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=False,
            )
        finally:
            os.close(log_fd)
        cfg.pid_path.write_text(str(self.process.pid))
        logger.info("[external] spawned pid=%s cmd=%s", self.process.pid, " ".join(cfg.command))

    @property
    def pid(self) -> int | None:
        if self.process is None:
            return None
        return self.process.pid

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def returncode(self) -> int | None:
        return self.process.returncode if self.process is not None else None

    async def wait(self) -> int | None:
        if self.process is None:
            return None
        return await self.process.wait()

    async def terminate(self) -> None:
        """SIGTERM → 宽限 → SIGKILL；对已退出进程幂等。"""
        proc = self.process
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=TERM_GRACE_S)
        except TimeoutError:
            logger.warning("[external] pid=%s 未在 %.1fs 内优雅退出，SIGKILL", proc.pid, TERM_GRACE_S)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=TERM_GRACE_S)
            except TimeoutError:
                logger.warning("[external] pid=%s SIGKILL 后仍未退出（不可达状态）", proc.pid)

    def drop(self) -> None:
        """丢弃进程句柄（进程已退出后调用）；清理 pid 文件。"""
        self.process = None
        with contextlib.suppress(OSError):
            self.config.pid_path.unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    """pid 是否对应存活进程（不发送信号）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限（不同用户），视为存活
    return True
