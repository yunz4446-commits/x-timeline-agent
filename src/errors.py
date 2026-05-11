"""统一错误码 — 工具返回 + LLM 调用。"""


class ErrorCode:
    RETRYABLE = "retryable"   # 临时的，重试可能恢复（超时、限流、网络闪断）
    PERMANENT = "permanent"   # 配置/参数错误，不会自动恢复
    DEGRADED = "degraded"     # 部分成功，降级可用（如缓存命中但新数据缺失）


def ok_result(data: dict | None = None) -> dict:
    r = {"ok": True}
    if data:
        r.update(data)
    return r


def err_result(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "error": message}
