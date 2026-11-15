"""本机调度唯一执行者；不同机器仍需只配置一个实例启用。"""

from pathlib import Path


class SchedulerLease:
    def __init__(self, path):
        self.path = Path(path)
        self._file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise RuntimeError("已有调度实例运行；其它实例须设置 DESKBOT_SCHEDULER_ENABLED=0") from exc
        self._file = handle

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
