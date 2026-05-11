"""长期记忆 — 提取、合并、检索。"""

import json
import logging

from ..db.repository import (
    save_memory, update_memory, search_similar_memory,
    get_latest_snapshot, get_corrections, cleanup_snapshots,
)
from ..search.embedding import encode

logger = logging.getLogger(__name__)


class MemoryManager:
    """记忆提取与存储。"""

    def __init__(self, llm_client, model: str, session, user_id: str):
        self._llm = llm_client
        self._model = model
        self._session = session
        self._user_id = user_id

    def extract_and_save(self, context_text: str) -> int:
        """从上下文文本提取记忆，做 embedding，合并相似，存入 DB。返回条数。"""
        items = self._extract(context_text)
        count = 0
        for item in items:
            mtype = item.get("type", "")
            content = item.get("content", "")
            if not mtype or not content:
                continue

            emb_str = ""
            try:
                vec = encode([content])[0]
                emb_str = json.dumps(vec.tolist(), ensure_ascii=False)
            except Exception:
                logger.warning("Embedding failed for memory: %s", content[:50])

            existing = search_similar_memory(
                self._session, self._user_id, mtype, emb_str)

            extra = {}
            if mtype == "topic_snapshot":
                extra["topic"] = item.get("topic", "")

            if existing:
                update_memory(self._session, existing.id, content, emb_str, extra)
            else:
                save_memory(self._session, self._user_id, mtype,
                            content, emb_str, extra)

            if mtype == "topic_snapshot":
                cleanup_snapshots(self._session, self._user_id)

            count += 1
        return count

    def _extract(self, text: str) -> list:
        """LLM 提取记忆 JSON 数组。"""
        prompt = (
            "分析以下对话和总结，提取可长期保留的信息。返回JSON数组。\n"
            "\n"
            "类型：\n"
            "- correction: 用户明确纠正agent的错误、给定新规则、要求改变行为。每条≤100字\n"
            "- topic_snapshot: 本次总结的话题结论（content含主题+讨论氛围+提及人数+日期，"
            "topic字段为主题名）\n"
            "\n"
            '返回示例：[{"type": "correction", "content": "搜索默认显示中文摘要"}, '
            '{"type": "topic_snapshot", "content": "2026-05-11 远程办公话题讨论热烈，15人参与", '
            '"topic": "远程办公"}]\n'
            "没有值得保留的信息则返回空数组：[]\n"
            "\n"
            f"输入：\n{text}\n"
            "\n"
            "JSON："
        )
        try:
            resp = self._llm.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500,
            )
            raw = (resp.choices[0].message.content or "").strip()
            items = json.loads(_strip_code_fence(raw))
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict) and i.get("content")]
        except (json.JSONDecodeError, TypeError):
            logger.warning("Memory extraction parse failed: %s", raw[:100])
        return []


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def get_topic_snapshot_context(session, user_id: str) -> str | None:
    """获取上次话题快照文本，喂给 summarize LLM 做对比。"""
    snap = get_latest_snapshot(session, user_id)
    if snap:
        ts = snap.created_at.strftime("%m/%d %H:%M") if snap.created_at else "?"
        return f"上次总结（{ts}）：{snap.content}"
    return None
