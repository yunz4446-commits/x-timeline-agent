"""工程化日志初始化 — 文件轮转 + 结构化上下文 + stderr。"""

import logging
import uuid
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .logging_context import _request_user_id, _request_id


class ContextFilter(logging.Filter):
    """将 contextvars 注入 LogRecord 的自定义字段。"""

    def filter(self, record):
        record.user_id = _request_user_id.get("?")
        record.request_id = _request_id.get("-")
        return True


_FILE_FMT = logging.Formatter(
    fmt="%(asctime)s [%(name)s] %(levelname)s [user:%(user_id)s] [req:%(request_id)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_STDERR_FMT = logging.Formatter(
    fmt="[%(name)s] %(levelname)s [user:%(user_id)s] %(message)s",
)


def setup_logging(log_level: str = "INFO", log_dir: str = "logs") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    cf = ContextFilter()

    # --- 文件：全量 INFO+，按天轮转 ---
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    app_handler = TimedRotatingFileHandler(
        filename=str(log_path / "app.log"),
        when="midnight",
        encoding="utf-8",
        backupCount=30,
    )
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(_FILE_FMT)
    app_handler.addFilter(cf)
    root.addHandler(app_handler)

    # --- 文件：ERROR+，按天轮转 ---
    err_handler = TimedRotatingFileHandler(
        filename=str(log_path / "error.log"),
        when="midnight",
        encoding="utf-8",
        backupCount=90,
    )
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(_FILE_FMT)
    err_handler.addFilter(cf)
    root.addHandler(err_handler)

    # --- stderr：全量，简洁格式 ---
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    stderr_handler.setFormatter(_STDERR_FMT)
    stderr_handler.addFilter(cf)
    root.addHandler(stderr_handler)


def generate_request_id() -> str:
    return uuid.uuid4().hex[:8]
