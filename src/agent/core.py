"""Agent core — LLM loop with tool use."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from openai import OpenAI

from .prompts import SYSTEM_PROMPT
from .tools import TOOLS_SCHEMA, SEARCH_X_TOOL, ToolExecutor
from ..db.engine import get_session
from ..db.repository import (
    save_conversation, get_recent_conversations,
    get_task_archives, delete_old_task_archives,
    get_corrections,
)
from .memory import MemoryManager
from ..logging_context import set_request_context
from ..logging_setup import generate_request_id
from ..metrics import call_with_metrics
from ..llm_retry import FALLBACK_MESSAGE

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


def _build_context_anchor(messages: list) -> str | None:
    """从工具调用消息中提取关键信息，生成上下文锚点。"""
    import json as _json
    parts = []
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        try:
            data = _json.loads(content)
        except (_json.JSONDecodeError, TypeError):
            continue

        # summarize_timeline
        if "topics" in data and "tweet_count" in data:
            tc = data.get("tweet_count", 0)
            topics = [t.get("topic", "") for t in data.get("topics", [])[:4]]
            topic_str = "/".join(topics) if topics else "无"
            parts.append(f"summarize: {tc}条, {len(topics)}话题({topic_str})")
        # query_timeline
        elif "trending" in data and "tweets" in data:
            tweets = data.get("tweets", [])
            max_s = max((t.get("score", 0) for t in tweets), default=0)
            parts.append(f"query: {len(tweets)}条高价值, 最高{max_s}")
        # search_timeline
        elif "method" in data and data.get("method") == "semantic":
            kw = "/".join(data.get("keywords", []))
            parts.append(f"search: {data.get('matched', 0)}条匹配, 关键词({kw})")
        # search_x_public
        elif "queries" in data and "total_scraped" in data:
            kw = "/".join(data.get("queries", []))
            parts.append(f"search_x: {data.get('total_scraped', 0)}条, 关键词({kw})")
        # fetch_timeline
        elif "new_tweets" in data:
            parts.append(f"fetch: {data.get('new_tweets', 0)}条新推文, {data.get('classified', 0)}条已分类")
        # get_tweet_texts (tweets 含 full_text 字段，无 trending/topics/method)
        elif "tweets" in data and "total" in data:
            ts = data.get("tweets", [])
            if ts and isinstance(ts[0], dict) and "full_text" in ts[0]:
                parts.append(f"原文: {data.get('total', 0)}条")
    return " | ".join(parts) if parts else None


class TimelineAgent:
    """Main agent that handles user messages via LLM + tool execution."""

    def __init__(self, api_key: str, api_base: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat", config=None):
        self._client = OpenAI(api_key=api_key, base_url=api_base, timeout=90.0, max_retries=2)
        self._model = model
        self._config = config

    def handle_message(self, user_message: str, user_id: str = "default") -> str:
        """Process a user message and return the agent response."""
        set_request_context(user_id, generate_request_id())
        session = get_session()
        try:
            messages = self._build_messages(user_id, user_message)
            tools = TOOLS_SCHEMA + [FETCH_TOOL, SEARCH_X_TOOL]
            if self._config:
                pass
            else:
                tools = TOOLS_SCHEMA

            pre_round_len = len(messages)  # 此轮之前的上下文边界
            summarize_called = False

            for round_idx in range(8):
                tc_mode = "auto"
                result = call_with_metrics(
                    self._client, self._model, "agent",
                    messages,
                    tools=tools,
                    tool_choice=tc_mode,
                    temperature=0.4,
                    max_tokens=8000,
                )
                if not result.get("ok"):
                    logger.error("LLM call failed in round %d: %s",
                                 round_idx, result.get("error"))
                    return FALLBACK_MESSAGE
                choice = result["response"].choices[0]
                msg = choice.message

                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        logger.info("Tool call: %s(%s)", tc.function.name, tc.function.arguments)
                        if tc.function.name == "summarize_timeline":
                            summarize_called = True
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

                    if summarize_called:
                        pre_context = messages[1:pre_round_len - 1]
                        # 归档上一任务
                        if pre_context:
                            archive = self._compress_messages(pre_context, "archive")
                            if archive:
                                save_conversation(session, user_id, "task_archive", archive)
                                delete_old_task_archives(session, user_id, keep=5)

                    anchor = _build_context_anchor(messages)
                    save_conversation(session, user_id, "user", user_message)
                    if anchor:
                        save_conversation(session, user_id, "system", anchor)
                    save_conversation(session, user_id, "assistant", answer)

                    # 提取长期记忆（summarize 之后）
                    if summarize_called and self._config:
                        extract_input = anchor or ""
                        if pre_context:
                            window_summary = self._compress_messages(pre_context, "window")
                            if window_summary:
                                extract_input = window_summary + "\n" + extract_input
                        if extract_input.strip():
                            try:
                                manager = MemoryManager(
                                    self._client, self._model, session, user_id)
                                extracted = manager.extract_and_save(extract_input)
                                if extracted:
                                    logger.info("Memory: extracted %d items", extracted)
                            except Exception as exc:
                                logger.warning("Memory extraction failed: %s", exc)

                    return answer

            return "(抱歉，处理超时，请稍后再试)"
        except Exception as exc:
            logger.exception("Agent error")
            return f"(出错了: {exc})"
        finally:
            session.close()

    def _build_messages(self, user_id: str, current_msg: str) -> list:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        system = SYSTEM_PROMPT.format(current_time=now)
        messages = [{"role": "system", "content": system}]

        session = get_session()
        try:
            # Inject correction rules (long-term memory)
            corrections = get_corrections(session, user_id)
            total_chars = 0
            for c in corrections:
                if total_chars + len(c.content) > 200:
                    break
                messages.append({"role": "system", "content": f"[规则] {c.content}"})
                total_chars += len(c.content)

            history = get_recent_conversations(session, user_id, limit=150)
            history = [h for h in history if h.role != "task_archive"]

            if not history:
                messages.append({"role": "user", "content": current_msg})
                return messages

            # Find last summarize_timeline anchor as task boundary
            task_start_idx = 0
            for i in range(len(history) - 1, -1, -1):
                h = history[i]
                if h.role == "system" and h.content.startswith("summarize:"):
                    task_start_idx = i
                    break

            current_task = history[task_start_idx:]

            # Count rounds (each user message = 1 round)
            user_indices = [i for i, h in enumerate(current_task) if h.role == "user"]

            if len(user_indices) <= 10:
                for h in current_task:
                    messages.append({"role": h.role, "content": h.content})
            else:
                split_at = user_indices[-10]  # keep last 10 rounds
                older = current_task[:split_at]
                recent = current_task[split_at:]

                compressed = self._compress_messages(older, "window")
                if compressed:
                    messages.append({"role": "system", "content": compressed})

                for h in recent:
                    messages.append({"role": h.role, "content": h.content})
        finally:
            session.close()

        messages.append({"role": "user", "content": current_msg})
        return messages

    def _compress_messages(self, messages, purpose: str) -> str:
        """用 LLM 将消息列表压缩为简短摘要。purpose='archive'|'window'"""
        parts = []
        for m in messages:
            role = m.role if hasattr(m, "role") else m.get("role", "")
            content = m.content if hasattr(m, "content") else m.get("content", "")
            if role == "system":
                parts.append(f"[数据锚点] {content}")
            elif role == "user":
                parts.append(f"用户: {content}")
            elif role == "assistant":
                parts.append(f"助手: {content}")
        transcript = "\n".join(parts)

        if purpose == "archive":
            instr = "请将以下对话记录压缩成一段简洁摘要（200字以内），概括用户问了什么问题、得到了哪些关键信息和结论。"
        else:
            instr = "请将以下对话记录压缩成一段简洁摘要（200字以内），保留关键数据和追问线索，确保后续对话不丢失重要上下文。"

        try:
            result = call_with_metrics(
                self._client, self._model, f"compress_{purpose}",
                [{"role": "user", "content": f"{instr}\n\n{transcript}\n\n摘要："}],
                temperature=0.2,
                max_tokens=500,
            )
            if result.get("ok"):
                return result["response"].choices[0].message.content or ""
            logger.warning("Compress LLM failed: %s", result.get("error"))
            return ""
        except Exception as exc:
            logger.warning("Compress failed: %s", exc)
            return ""


