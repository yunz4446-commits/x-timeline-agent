"""Tools that the agent can call."""

import json
import logging
from datetime import datetime, timezone, timedelta

from openai import OpenAI
from sqlalchemy.orm import Session

from ..db.repository import (
    get_useful_tweets, get_trending_topics,
    add_bookmark, get_bookmarks, delete_bookmark,
)

logger = logging.getLogger(__name__)

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "summarize_timeline",
            "description": (
                "深度总结时间线。阅读从上次总结到现在的全部推文原文，"
                "找出反复出现的币种/项目/事件/人物，归纳不同人的具体观点并交叉对比。"
                "用户说'总结''最近发生了什么''有什么热点'时优先使用。"
                "耗时较长（需要调用LLM阅读全部推文）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_timeline",
            "description": (
                "查看高价值推文（按有用度排序）。返回过去若干小时内的优质推文+热门话题。"
                "不提供关键词搜索，仅按有用度筛选展示。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "最近多少小时，默认6"},
                    "limit": {"type": "integer", "description": "最多返回几条，默认10"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_timeline",
            "description": (
                "语义搜索推文（基于向量相似度）。支持同义词、近义表达、模糊查询。"
                "想查更多结果时传更大的 limit（默认50）。"
                "想扩大时间范围时传更大的 days（默认7天，最小7）。"
                "days 增大时 max_candidates 自动等比放大，也可手动设更大的值。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词（必填）"},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认50"},
                    "days": {"type": "integer", "description": "搜索最近多少天，默认7，最小7，最大90"},
                    "max_candidates": {"type": "integer", "description": "候选池大小，默认1500。days 增大时自动等比放大。只可调大不可调小。"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bookmark_tweet",
            "description": "收藏一条推文为稍后阅读，自动保存推文内容、作者、链接",
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "推文ID"},
                    "note": {"type": "string", "description": "备注，可选"},
                },
                "required": ["tweet_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_bookmarks",
            "description": "查看所有收藏的推文列表，含推文内容、作者、链接、收藏时间",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unbookmark",
            "description": "取消收藏一条推文",
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "要取消收藏的推文ID"},
                },
                "required": ["tweet_id"],
            },
        },
    },
]

SEARCH_X_TOOL = {
    "type": "function",
    "function": {
        "name": "search_x_public",
        "description": (
            "全平台搜索X广场（按热度排序）。打开浏览器访问 x.com/search，"
            "返回全X平台的热门讨论，不限于已关注的账号。结果同时保存到数据库。"
            "仅在用户明确追问、要求扩大搜索范围时使用。不要作为首次搜索工具。"
            "即使 search_timeline 返回结果很少也不要自动调用。"
            "比 search_timeline 慢（需打开浏览器+分类），但覆盖面广。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词（必填）。X搜索框一样的格式。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回多少条，默认30",
                },
            },
            "required": ["query"],
        },
    },
}


class ToolExecutor:
    """Execute tool calls from the LLM."""

    def __init__(self, session: Session, user_id: str = "default", config=None):
        self._session = session
        self._user_id = user_id
        self._config = config
        if config and config.llm_api_key:
            self._llm_client = OpenAI(
                api_key=config.llm_api_key, base_url=config.llm_api_base,
                timeout=90.0, max_retries=2)
        else:
            self._llm_client = None

    def execute(self, name: str, args: dict) -> str:
        handlers = {
            "summarize_timeline": self._summarize_timeline,
            "query_timeline": self._query_timeline,
            "search_timeline": self._search_timeline,
            "bookmark_tweet": self._bookmark_tweet,
            "list_bookmarks": self._list_bookmarks,
            "unbookmark": self._unbookmark,
            "fetch_timeline": self._fetch_timeline,
            "search_x_public": self._search_x_public,
        }
        handler = handlers.get(name)
        if not handler:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            result = handler(**args)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

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

    @staticmethod
    def _get_last_summary_time() -> datetime | None:
        """读取上次总结时间，用于统一 query 和 summarize 的时间窗口。"""
        try:
            from pathlib import Path
            state = json.loads(Path("data/summary.json").read_text(encoding="utf-8"))
            ts = state.get("last_summary_at")
            if ts:
                dt = datetime.fromisoformat(ts)
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except Exception:
            pass
        return None

    def _query_timeline(self, hours: int = 6, limit: int = 10) -> dict:
        """高价值推文展示 + 热门话题。"""
        result = {}

        base_since = self._get_last_summary_time()
        fallback_since = datetime.now(timezone.utc) - timedelta(hours=hours)
        if base_since is None:
            base_since = fallback_since

        tweets = get_useful_tweets(self._session, min_score=0.5,
                                    limit=limit, since=base_since)
        effective_since = base_since
        if not tweets and base_since > fallback_since:
            tweets = get_useful_tweets(self._session, min_score=0.5,
                                        limit=limit, since=fallback_since)
            effective_since = fallback_since
        result["tweets"] = [
            {
                "author": t.author_username,
                "text": t.summary_zh or (t.text or "")[:200],
                "score": self._get_usefulness(t),
                "reason": self._get_reason(t),
                "time": str(t.tweet_created_at),
                "link": t.link_url if t.has_link else f"https://x.com/{t.author_username}/status/{t.tweet_id}",
                "tweet_id": t.tweet_id,
                "has_link": t.has_link,
            }
            for t in tweets
        ]

        actual_hours = max(1, (datetime.now(timezone.utc) - effective_since).total_seconds() / 3600)
        trending = get_trending_topics(self._session, hours=int(actual_hours), top_n=5)
        result["trending"] = [
            {
                "author": t.author_username,
                "text": t.summary_zh or (t.text or "")[:200],
                "score": round(t.like_count + t.retweet_count * 2, 0),
                "link": f"https://x.com/{t.author_username}/status/{t.tweet_id}",
            }
            for t in trending
        ]

        return result

    def _search_timeline(self, keyword: str, limit: int = 50,
                         days: int = 7, max_candidates: int = 0) -> dict:
        """语义搜索：向量相似度匹配，不限关键词精确匹配."""
        from ..db.repository import semantic_search

        days = max(7, min(days, 90))   # floor 7, ceiling 90
        base_candidates = 1500
        scaled = max(base_candidates, base_candidates * days // 7)
        max_candidates = max(scaled, max_candidates)

        results = semantic_search(self._session, keyword,
                                  top_k=limit, days=days,
                                  max_candidates=max_candidates)
        return {
            "results": [
                {
                    "author": r["author_username"],
                    "text": r.get("summary_zh") or r["text"][:300],
                    "time": str(r["tweet_created_at"]),
                    "link": r["link_url"] if r.get("has_link") else f"https://x.com/{r['author_username']}/status/{r['tweet_id']}",
                    "tweet_id": r["tweet_id"],
                    "score": r.get("score", 0),
                }
                for r in results
            ],
            "matched": len(results),
            "limit": limit,
            "days": days,
            "max_candidates": max_candidates,
            "method": "semantic",
        }

    def _bookmark_tweet(self, tweet_id: str, note: str = "") -> dict:
        """收藏推文，从 Tweet 表读取详情并自存到 Bookmark。"""
        from ..db.models import Tweet
        tweet = self._session.query(Tweet).filter_by(tweet_id=tweet_id).first()
        if tweet:
            author = tweet.author_username or ""
            text = tweet.summary_zh or (tweet.text or "")[:500]
            link = tweet.link_url if tweet.has_link else f"https://x.com/{tweet.author_username}/status/{tweet.tweet_id}"
            score = self._get_usefulness(tweet)
            ts = tweet.tweet_created_at
            source = "database"
        else:
            author, text, link, score, ts = "", "", "", 0.0, None
            source = "user_context"
        add_bookmark(
            self._session, self._user_id, tweet_id, note=note,
            author_username=author, text=text, link=link,
            tweet_created_at=ts, score=score)
        ts_str = str(ts) if ts else ""
        return {
            "status": "ok",
            "tweet_id": tweet_id,
            "author": author,
            "text": text,
            "link": link,
            "score": score,
            "tweet_created_at": ts_str,
            "note": note,
            "source": source,
        }

    def _list_bookmarks(self, **kwargs) -> dict:
        bookmarks = get_bookmarks(self._session, self._user_id)
        has_incomplete = any(
            not bm.author_username or not bm.text or not bm.link
            for bm in bookmarks
        )
        result = {
            "bookmarks": [
                {
                    "tweet_id": bm.tweet_id,
                    "author": bm.author_username,
                    "text": bm.text,
                    "link": bm.link,
                    "score": bm.score,
                    "note": bm.note,
                    "tweet_created_at": str(bm.tweet_created_at) if bm.tweet_created_at else "",
                    "bookmarked_at": str(bm.created_at),
                }
                for bm in bookmarks
            ],
            "total": len(bookmarks),
        }
        if has_incomplete:
            result["has_incomplete"] = True
            result["_hint"] = "部分书签信息来自用户口述，未在推文数据库中找到原文。author/text/link 为空但 note 有内容的属于此类。"
        return result

    def _unbookmark(self, tweet_id: str, **kwargs) -> dict:
        deleted = delete_bookmark(self._session, self._user_id, tweet_id)
        return {"status": "ok" if deleted else "not_found", "tweet_id": tweet_id}

    def _summarize_timeline(self, **kwargs) -> dict:
        """深度总结：阅读全量推文原文，发现热点话题，归纳观点，交叉引用。"""
        if not self._llm_client or not self._config:
            return {"error": "LLM 不可用，无法执行深度总结"}
        from .summarize import SummarizeManager
        manager = SummarizeManager()
        return manager.summarize(
            llm=self._llm_client,
            model=self._config.llm_model,
            session=self._session,
        )

    def _fetch_timeline(self, since_hours: int = 3) -> dict:
        if not self._config:
            return {"error": "抓取功能不可用，请使用 python main.py fetch 手动抓取"}
        from ..scheduler.jobs import fetch_timeline_job, classify_tweets_job
        from ..db.models import Tweet
        from ..db.engine import get_session as new_session
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        fetch_timeline_job(self._config, since=since, jitter=False)
        classify_tweets_job(self._config)
        s = new_session()
        try:
            count = s.query(Tweet).filter(
                Tweet.fetched_at >= since
            ).count()
            classified_now = s.query(Tweet).filter(
                Tweet.fetched_at >= since,
                Tweet.category_scores != "{}"
            ).count()
        finally:
            s.close()
        return {"status": "ok", "new_tweets": count, "classified": classified_now, "fetched_since": str(since)}

    def _search_x_public(self, query: str, max_results: int = 30) -> dict:
        """Search X.com public square via browser, save to DB, classify, return results."""
        if not self._config:
            return {"error": "搜索功能不可用，浏览器未配置"}
        from ..browser.browser import XBrowser
        from ..browser.timeline import TimelineScraper
        from ..db.repository import insert_tweet
        from ..db.engine import get_session as new_session
        from ..scheduler.jobs import classify_tweets_job

        browser = XBrowser(headless=True)
        try:
            browser.start()
            if not browser.load_session():
                return {"error": "未找到X登录会话。请运行: python main.py login"}
            scraper = TimelineScraper(browser)
            tweets = scraper.search(query, max_tweets=max_results)
            if tweets is None:
                return {"error": "X会话已过期。请运行: python main.py login 重新登录"}
            if not tweets:
                return {
                    "query": query,
                    "total_scraped": 0,
                    "saved_to_db": 0,
                    "results": [],
                    "message": "X搜索未返回任何结果",
                }

            s = new_session()
            try:
                saved_count = 0
                for st in tweets:
                    data = {
                        "tweet_id": st.tweet_id,
                        "author_id": st.author_username,
                        "author_username": st.author_username,
                        "text": st.text,
                        "lang": st.lang,
                        "is_retweet": st.is_retweet,
                        "is_reply": st.is_reply,
                        "reply_to_username": st.reply_to_username,
                        "like_count": st.like_count,
                        "retweet_count": st.retweet_count,
                        "reply_count": st.reply_count,
                        "tweet_created_at": st.tweet_created_at,
                        "raw": {"display_name": st.author_display_name,
                                "link_urls": st.link_urls, "views": st.view_count},
                    }
                    if insert_tweet(s, data):
                        saved_count += 1
            finally:
                s.close()

            for _ in range(2):
                classify_tweets_job(self._config)

            results = [
                {
                    "author": t.author_username,
                    "text": t.text[:300],
                    "time": str(t.tweet_created_at),
                    "link": f"https://x.com/{t.author_username}/status/{t.tweet_id}",
                    "tweet_id": t.tweet_id,
                    "likes": t.like_count,
                    "retweets": t.retweet_count,
                }
                for t in tweets
            ]
            return {
                "query": query,
                "total_scraped": len(tweets),
                "saved_to_db": saved_count,
                "results": results,
            }
        finally:
            browser.stop()
