import logging
import logging.handlers
import os
import sys
import time


class _MillisecondFormatter(logging.Formatter):
    """日志时间戳精确到毫秒（3 位）。"""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        ct = self.converter(record.created)
        base = time.strftime("%Y-%m-%d %H:%M:%S", ct)
        return f"{base}.{int(record.msecs):03d}"


def setup_logging(log_file: str | None = None) -> None:
    """初始化日志：控制台 + 可选文件。

    Args:
        log_file: 日志文件路径。传 None 时从环境变量 ``DESKBOT_SERVER_LOG_FILE`` 读取。
    """
    level_name = (os.environ.get("DESKBOT_SERVER_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    fmt = _MillisecondFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    resolved = (os.environ.get("DESKBOT_SERVER_LOG_FILE") or log_file or "").strip()
    if resolved:
        # 轮转：默认单文件 100MB × 3 份备份（总 ≤400MB），防 app.log 无限膨胀
        # （实测 pb 帧序等高频 INFO 日志一天可达 GB 级）
        max_bytes = max(1 << 20, int(os.environ.get("DESKBOT_LOG_MAX_BYTES", str(100 * 1024 * 1024))))
        backups = max(0, int(os.environ.get("DESKBOT_LOG_BACKUP_COUNT", "3")))
        fh = logging.handlers.RotatingFileHandler(
            resolved, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
