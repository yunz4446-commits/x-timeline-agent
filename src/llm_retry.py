"""LLM 调用重试封装 + 规则兜底。"""

import logging
import time

from openai import (
    APITimeoutError, RateLimitError, APIConnectionError,
    InternalServerError,
)

from .errors import ErrorCode

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = "(服务暂时不可用，请稍后重试)"

RETRYABLE = (
    APITimeoutError, RateLimitError, APIConnectionError, InternalServerError,
)


def llm_call_with_retry(client, model: str, messages: list,
                        max_retries: int = 3, **kwargs) -> dict:
    """带指数退避的 LLM 调用。

    Returns:
        {"ok": True, "response": completion}
        {"ok": False, "error": "...", "code": ErrorCode.RETRYABLE|PERMANENT}
    """
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, **kwargs)
            return {"ok": True, "response": resp}
        except RETRYABLE as exc:
            if attempt < max_retries - 1:
                delay = 2 ** attempt
                logger.warning(
                    "LLM call retryable (attempt %d/%d), retry in %ds: %s",
                    attempt + 1, max_retries, delay, exc)
                time.sleep(delay)
            else:
                logger.exception("LLM call failed after %d retries", max_retries)
                return {"ok": False, "code": ErrorCode.RETRYABLE,
                        "error": str(exc)}
        except Exception as exc:
            logger.exception("LLM call permanent error")
            return {"ok": False, "code": ErrorCode.PERMANENT,
                    "error": str(exc)}
    return {"ok": False, "code": ErrorCode.RETRYABLE,
            "error": "max retries exceeded"}
