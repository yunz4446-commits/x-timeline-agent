"""Digest builder — deep summary + usefulness-ranked tweets."""

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session
from openai import OpenAI

from ..db.repository import (
    get_useful_tweets, get_all_tweets_since, mark_tweet_in_digest, log_digest,
)

MIN_SCORE = 0.5
MAX_ITEMS = 20


class DigestBuilder:
    def __init__(self, session: Session, since: Optional[datetime] = None,
                 min_score: float = MIN_SCORE, max_per: int = MAX_ITEMS,
                 llm_api_key: str = "", llm_api_base: str = "https://api.deepseek.com",
                 llm_model: str = "deepseek-chat"):
        self._session = session
        self._since = since or (datetime.now(timezone.utc) - timedelta(hours=12))
        self._min_score = min_score
        self._max_items = max_per
        self._llm_key = llm_api_key
        self._llm_base = llm_api_base
        self._llm_model = llm_model
        self._summary_result: dict | None = None

    def build(self) -> dict:
        # 1. 深度总结：阅读该时段全部推文原文
        self._summary_result = self._build_deep_summary()

        # 2. 高价值推文展示
        tweets = get_useful_tweets(
            self._session, min_score=self._min_score, limit=500, since=self._since)

        if self._since:
            filtered = []
            for t in tweets:
                if not t.tweet_created_at:
                    filtered.append(t)
                    continue
                tc = t.tweet_created_at
                if tc.tzinfo is None:
                    tc = tc.replace(tzinfo=timezone.utc)
                _since = self._since
                if _since.tzinfo is None:
                    _since = _since.replace(tzinfo=timezone.utc)
                if tc >= _since:
                    filtered.append(t)
            tweets = filtered

        tweets = [t for t in tweets if not t.in_digest]
        tweets = tweets[:self._max_items]

        result = {
            "tweets": tweets,
            "tweet_ids": [t.tweet_id for t in tweets],
            "total": len(tweets),
            "summary": self._summary_result,
        }
        return result

    def _build_deep_summary(self) -> dict:
        """阅读全量推文原文，生成带话题归纳和交叉引用的深度总结。"""
        if not self._llm_key:
            return {}
        all_tweets = get_all_tweets_since(self._session, self._since)
        valid = [t for t in all_tweets if t.text and t.text.strip()]
        if not valid:
            return {}

        from ..agent.summarize import SummarizeManager
        llm = OpenAI(api_key=self._llm_key, base_url=self._llm_base, timeout=90.0, max_retries=2)
        manager = SummarizeManager()
        return manager.summarize(
            llm=llm,
            model=self._llm_model,
            session=self._session,
            since=self._since,
        )

    def build_markdown(self, digest: dict) -> str:
        lines = []
        lines.append("\U0001f4ca **" + self._describe_period() + "**")
        lines.append("")

        # 深度总结：概览 + 热点话题
        summary = digest.get("summary", {})
        if summary and summary.get("overview"):
            lines.append("\U0001f4e2 **整体概括**")
            lines.append("")
            lines.append(summary["overview"])
            lines.append("")

            topics = summary.get("topics", [])
            if topics:
                sentiment_map = {"积极": "\U0001f7e2", "消极": "\U0001f534",
                                 "分歧": "\U0001f7e0", "中性": "\U0001f7e1",
                                 "偏多": "\U0001f7e2", "偏空": "\U0001f534",
                                 "观望": "\U0001f7e1"}
                for tp in topics:
                    emoji = sentiment_map.get(tp.get("sentiment", ""), "")
                    lines.append(
                        f"**\U0001f539 {tp['topic']}** "
                        f"（{tp['mention_count']}条）{emoji}{tp.get('sentiment', '')}"
                    )
                    for p in tp.get("perspectives", []):
                        lines.append(
                            f"  · @{p['author']}（{p.get('time', '')}）：{p['angle']}"
                        )
                    if tp.get("cross_ref"):
                        lines.append(f"  → {tp['cross_ref']}")
                    lines.append("")

        # 推文展示
        tweets = digest.get("tweets", [])
        high = [t for t in tweets if self._get_usefulness(t) >= 0.8]
        mid = [t for t in tweets if 0.6 <= self._get_usefulness(t) < 0.8]

        if high or mid:
            lines.append("─" * 20)
            lines.append("")

        if high:
            lines.append("\U0001f7e2 **高价值** (≥0.8)")
            for t in high:
                lines.append(self._format_tweet(t))
            lines.append("")

        if mid:
            lines.append("\U0001f7e1 **值得关注** (0.6-0.8)")
            for t in mid:
                lines.append(self._format_tweet(t))
            lines.append("")

        if digest["total"] == 0:
            lines.append("本时段暂无值得关注的推文")

        lines.append("─" * 20)
        lines.append("\U0001f4a1 以上为 AI 筛选结果")
        lines.append("共 " + str(digest["total"]) + " 条精选推文")
        return chr(10).join(lines)

    def _format_tweet(self, tweet) -> str:
        author = tweet.author_username or "unknown"
        text = (tweet.summary_zh or tweet.text or "")[:200]
        usefulness = self._get_usefulness(tweet)
        reason = self._get_reason(tweet)
        ts = ""
        if tweet.tweet_created_at:
            ts = tweet.tweet_created_at.strftime("%H:%M")
        line = "\U0001f539 **@" + author + "** (" + ts + ") [" + str(round(usefulness, 1)) + "]: " + text
        if reason:
            line = line + chr(10) + "    \U0001f4ac " + reason
        if tweet.link_url:
            line = line + chr(10) + "    \U0001f517 " + tweet.link_url
        return line

    @staticmethod
    def _get_usefulness(tweet) -> float:
        try:
            return json.loads(tweet.category_scores).get("usefulness", 0)
        except (json.JSONDecodeError, TypeError):
            return 0

    @staticmethod
    def _get_reason(tweet) -> str:
        try:
            return json.loads(tweet.category_scores).get("reason", "")
        except (json.JSONDecodeError, TypeError):
            return ""

    def _describe_period(self) -> str:
        start = self._since.strftime("%m/%d %H:%M") if self._since else "?"
        end = datetime.now(timezone.utc).strftime("%H:%M")
        return start + " - " + end + " 时间线摘要"

    def commit(self, digest: dict, period: str) -> None:
        if digest["tweet_ids"]:
            mark_tweet_in_digest(self._session, digest["tweet_ids"])
        log_digest(self._session, period, digest["total"])

    @staticmethod
    def period_for_hour(hour: int) -> str:
        if hour < 12:
            return "10:00"
        return "17:00"
