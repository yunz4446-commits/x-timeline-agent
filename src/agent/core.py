"""Agent core — LLM loop with tool use."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from openai import OpenAI

from .prompts import SYSTEM_PROMPT
from .tools import TOOLS_SCHEMA, SEARCH_X_TOOL, ToolExecutor
from ..db.engine import get_session
from ..db.repository import save_conversation, get_recent_conversations

logger = logging.getLogger(__name__)

FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_timeline",
        "description": "抓取最新的时间线推文。当用户想看最新内容、刷新时间线、或者现有数据太旧时使用。会打开浏览器抓取、分类、入库。",
        "parameters": {
            "type": "object",
            "properties": {
                "since_hours": {
                    "type": "integer",
                    "description": "抓取最近多少小时的推文，默认3",
                },
            },
            "required": [],
        },
    },
}


class TimelineAgent:
    """Main agent that handles user messages via LLM + tool execution."""

    def __init__(self, api_key: str, api_base: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat", config=None):
        self._client = OpenAI(api_key=api_key, base_url=api_base, timeout=90.0, max_retries=2)
        self._model = model
        self._config = config

    def handle_message(self, user_message: str, user_id: str = "default") -> str:
        """Process a user message and return the agent response."""
        session = get_session()
        try:
            messages = self._build_messages(user_id, user_message)
            tools = TOOLS_SCHEMA + [FETCH_TOOL, SEARCH_X_TOOL]
            if self._config:
                pass
            else:
                tools = TOOLS_SCHEMA

            for round_idx in range(8):
                tc_mode = "required" if round_idx == 0 else "auto"
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tc_mode,
                    temperature=0.4,
                    max_tokens=8000,
                )
                choice = resp.choices[0]
                msg = choice.message

                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        logger.info("Tool call: %s(%s)", tc.function.name, tc.function.arguments)
                    messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                            }
                            for tc in msg.tool_calls
                        ],
                    })
                    executor = ToolExecutor(session, user_id, self._config)
                    for tc in msg.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            args = {}
                        result = executor.execute(tc.function.name, args)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
                else:
                    answer = msg.content or ""
                    logger.info("Agent answer (no tool call): %s...", answer[:80])
                    save_conversation(session, user_id, "user", user_message)
                    save_conversation(session, user_id, "assistant", answer)
                    return answer

            return "(抱歉，处理超时，请稍后再试)"
        except Exception as exc:
            logger.error("Agent error: %s", exc)
            return f"(出错了: {exc})"
        finally:
            session.close()

    def _build_messages(self, user_id: str, current_msg: str) -> list:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        system = SYSTEM_PROMPT.format(current_time=now)
        messages = [{"role": "system", "content": system}]

        session = get_session()
        try:
            history = get_recent_conversations(session, user_id, limit=16)
            for h in history:
                messages.append({"role": h.role, "content": h.content})
        finally:
            session.close()

        messages.append({"role": "user", "content": current_msg})
        return messages


