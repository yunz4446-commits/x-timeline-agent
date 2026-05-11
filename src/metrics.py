"""LLM 调用指标 — token 消耗 + 延迟 + 成本预估。"""

import logging
import time

logger = logging.getLogger(__name__)

# 粗略成本估算（RMB/1M tokens），价格可能变动
COST_PER_1M = {
    "deepseek-chat": (0.004, 0.016),       # prompt, completion
    "deepseek-reasoner": (0.016, 0.032),
}


def log_metrics(model: str, purpose: str, latency: float,
                prompt_tokens: int, completion_tokens: int) -> None:
    """记录 LLM 调用指标到日志。"""
    costs = COST_PER_1M.get(model, (0, 0))
    cost = (prompt_tokens * costs[0] + completion_tokens * costs[1]) / 1_000_000
    logger.info(
        "[metrics] model=%s purpose=%s tokens=%d+%d latency=%.1fs cost=$%.5f",
        model, purpose, prompt_tokens, completion_tokens, latency, cost,
    )


def call_with_metrics(client, model: str, purpose: str,
                      messages: list, **kwargs) -> dict:
    """调用 LLM 并自动记录指标。

    Returns:
        {"ok": True, "response": completion}
        {"ok": False, "error": ..., "code": ...}
    """
    from .llm_retry import llm_call_with_retry
    t0 = time.time()
    result = llm_call_with_retry(client, model, messages, **kwargs)
    elapsed = time.time() - t0

    if result.get("ok") and result.get("response"):
        usage = getattr(result["response"], "usage", None)
        if usage:
            log_metrics(
                model=model, purpose=purpose, latency=elapsed,
                prompt_tokens=getattr(usage, "prompt_tokens", 0),
                completion_tokens=getattr(usage, "completion_tokens", 0),
            )
    return result
