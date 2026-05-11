"""请求级日志上下文 — 基于 contextvars，线程/协程安全。"""

import contextvars

_request_user_id = contextvars.ContextVar("user_id", default="?")
_request_id = contextvars.ContextVar("request_id", default="-")


def set_request_context(user_id: str, request_id: str) -> None:
    _request_user_id.set(user_id)
    _request_id.set(request_id)
