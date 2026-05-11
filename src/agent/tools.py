"""Tools that the agent can call."""

import json
import logging
from datetime import datetime, timezone, timedelta

from openai import OpenAI
from sqlalchemy.orm import Session

from ..errors import ErrorCode, err_result, ok_result
from ..db.repository import (
    get_useful_tweets, get_trending_topics, get_tweets_by_ids,
    add_bookmark, get_bookmarks, delete_bookmark,
    get_task_archives,
    list_memories, delete_memory, clear_memories_by_type,
)

logger = logging.getLogger(__name__)

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "summarize_timeline",
            "description": (
                "深度总结时间线。阅读从上次总结到现在的全部推文原文，"
                "找出反复出现的话题/项目/事件/人物，归纳不同人的具体观点并交叉对比。"
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
                    "hours": {"type": "integer", "description": "限定最近多少小时。不传则从上次总结开始到现在"},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认100。与 summarize 配合时传10"},
                    "full_text": {"type": "boolean", "description": "是否返回完整原文。默认false返回摘要。用户明确要求原文/全文时才传true"},
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
                    "keyword": {"type": "string", "description": "中文搜索关键词（必填）"},
                    "query_en": {"type": "string", "description": "英文搜索关键词（必填），对应中文关键词的英文翻译"},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认50"},
                    "days": {"type": "integer", "description": "搜索最近多少天，默认7，最小7，最大90。与 hours 互斥——如果提供了 hours 则忽略 days"},
                    "hours": {"type": "integer", "description": "搜索最近多少小时，例如6、12、24。提供后自动换算为天数，绕过7天最小限制。不与 days 同时使用"},
                    "full_text": {"type": "boolean", "description": "是否返回完整原文。默认false返回摘要。用户明确要求原文/全文时才传true"},
                    "max_candidates": {"type": "integer", "description": "候选池大小，默认1500。days 增大时自动等比放大。只可调大不可调小。"},
                },
                "required": ["keyword", "query_en"],
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
    {
        "type": "function",
        "function": {
            "name": "get_tweet_texts",
            "description": (
                "批量获取推文完整原文。用于用户追问'把以上推文原文给我''展开这条''看全文'等场景。"
                "从上一轮返回结果中提取 tweet_id 列表传入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tweet_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "推文ID列表，从上一轮返回结果的 tweet_id 字段提取",
                    },
                },
                "required": ["tweet_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_context",
            "description": (
                "回溯历史对话。检索之前总结/讨论的归档摘要。"
                "当用户问'上次总结说了什么''之前讨论了什么''回顾一下'时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回最近几条归档，默认3"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": (
                "管理长期记忆。查看/删除/清除agent记住的规则和话题快照。"
                "用户说'查看记忆''删掉那条规则''清除纠错规则'时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "forget", "clear"],
                        "description": "list=查看所有记忆, forget=删除某条(需传memory_id), clear=清除某类(需传type)",
                    },
                    "memory_id": {"type": "integer", "description": "要删除的记忆ID，action=forget时传"},
                    "type": {"type": "string", "description": "要清除的类型 correction/topic_snapshot，action=clear时传"},
                },
                "required": ["action"],
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
                    "description": "中文搜索关键词（必填）",
                },
                "query_en": {
                    "type": "string",
                    "description": "英文搜索关键词（必填），如 climate change、Python、open source",
                },
                "max_results": {
                    "type": "integer",
                    "description": "每条 query 最多返回多少条，默认20",
                },
            },
            "required": ["query", "query_en"],
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
            "get_tweet_texts": self._get_tweet_texts,
            "fetch_timeline": self._fetch_timeline,
            "search_x_public": self._search_x_public,
            "recall_context": self._recall_context,
            "manage_memory": self._manage_memory,
        }
        handler = handlers.get(name)
        if not handler:
            return json.dumps(err_result(ErrorCode.PERMANENT, f"Unknown tool: {name}"),
                              ensure_ascii=False)
        try:
            result = handler(**args)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps(err_result(ErrorCode.RETRYABLE, str(exc)),
                              ensure_ascii=False)

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

    def _query_timeline(self, hours: int = 0, limit: int = 100,
                         full_text: bool = False) -> dict:
        """高价值推文展示 + 热门话题。"""
        result = {}

        if hours > 0:
            since = datetime.now(timezone.utc) - timedelta(hours=hours)
        else:
            since = self._get_last_summary_time()
            if since is None:
                since = datetime.now(timezone.utc) - timedelta(hours=6)

        tweets = get_useful_tweets(self._session, min_score=0.5,
                                    limit=limit, since=since)
        result["tweets"] = [
            {
                "author": t.author_username,
                "text": (t.text if full_text
                         else (t.summary_zh or (t.text or "")[:200])),
                "score": self._get_usefulness(t),
                "reason": self._get_reason(t),
                "time": str(t.tweet_created_at),
                "link": t.link_url if t.has_link else f"https://x.com/{t.author_username}/status/{t.tweet_id}",
                "tweet_id": t.tweet_id,
                "has_link": t.has_link,
            }
            for t in tweets
        ]

        actual_hours = max(1, (datetime.now(timezone.utc) - since).total_seconds() / 3600)
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

    def _search_timeline(self, keyword: str, query_en: str = "",
                         limit: int = 50, days: int = 7, max_candidates: int = 0,
                         hours: int = 0, full_text: bool = False) -> dict:
        """语义搜索：支持中英双语 query，合并去重后按 score 降序."""
        from ..db.repository import semantic_search

        if hours > 0:
            days = hours / 24.0
        else:
            days = max(7, min(days, 90))
        base_candidates = 1500
        scaled = max(base_candidates, int(base_candidates * days / 7))
        max_candidates = max(scaled, max_candidates)

        keywords = list(dict.fromkeys(k for k in [keyword, query_en] if k))

        all_results = {}  # tweet_id -> result dict
        for kw in keywords:
            results = semantic_search(self._session, kw,
                                      top_k=limit, days=days,
                                      max_candidates=max_candidates)
            for r in results:
                tid = r["tweet_id"]
                if tid not in all_results or r.get("score", 0) > all_results[tid].get("score", 0):
                    all_results[tid] = r

        merged = sorted(all_results.values(),
                       key=lambda r: r.get("score", 0), reverse=True)[:limit]

        return {
            "results": [
                {
                    "author": r["author_username"],
                    "text": (r["text"] if full_text
                             else (r.get("summary_zh") or r["text"][:300])),
                    "time": str(r["tweet_created_at"]),
                    "link": r["link_url"] if r.get("has_link") else f"https://x.com/{r['author_username']}/status/{r['tweet_id']}",
                    "tweet_id": r["tweet_id"],
                    "score": r.get("score", 0),
                }
                for r in merged
            ],
            "matched": len(merged),
            "keywords": keywords,
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

    def _get_tweet_texts(self, tweet_ids: list[str]) -> dict:
        """批量取原文。"""
        if not tweet_ids:
            return {"tweets": [], "total": 0}
        tweets = get_tweets_by_ids(self._session, tweet_ids)
        return {
            "tweets": [
                {
                    "tweet_id": t.tweet_id,
                    "author": t.author_username,
                    "full_text": t.text or "",
                    "time": str(t.tweet_created_at) if t.tweet_created_at else "",
                    "link": t.link_url if t.has_link else f"https://x.com/{t.author_username}/status/{t.tweet_id}",
                }
                for t in tweets
            ],
            "total": len(tweets),
        }

    def _recall_context(self, limit: int = 3) -> dict:
        """检索历史任务归档摘要。"""
        archives = get_task_archives(self._session, self._user_id, limit=limit)
        return {
            "archives": [
                {
                    "time": str(a.created_at) if a.created_at else "",
                    "summary": a.content,
                }
                for a in archives
            ],
            "total": len(archives),
        }

    def _manage_memory(self, action: str, memory_id: int = 0,
                       type: str = "") -> dict:
        """管理长期记忆。"""
        if action == "list":
            mems = list_memories(self._session, self._user_id)
            return {
                "memories": [
                    {
                        "id": m.id,
                        "type": m.type,
                        "content": m.content,
                        "weight": m.weight,
                        "updated_at": str(m.updated_at) if m.updated_at else "",
                    }
                    for m in mems
                ],
                "total": len(mems),
            }
        elif action == "forget":
            if not memory_id:
                return err_result(ErrorCode.PERMANENT, "需要指定 memory_id")
            ok = delete_memory(self._session, memory_id)
            return {"deleted": ok}
        elif action == "clear":
            if type not in ("correction", "topic_snapshot"):
                return err_result(ErrorCode.PERMANENT, "type 必须是 correction 或 topic_snapshot")
            deleted = clear_memories_by_type(self._session, self._user_id, type)
            return {"deleted": deleted}
        return err_result(ErrorCode.PERMANENT, f"未知 action: {action}")

    def _summarize_timeline(self, **kwargs) -> dict:
        """深度总结：阅读全量推文原文，发现热点话题，归纳观点，交叉引用。"""
        if not self._llm_client or not self._config:
            return err_result(ErrorCode.PERMANENT, "LLM 不可用，无法执行深度总结")
        from .summarize import SummarizeManager
        from .memory import get_topic_snapshot_context
        hint = get_topic_snapshot_context(self._session, self._user_id) or ""
        manager = SummarizeManager()
        return manager.summarize(
            llm=self._llm_client,
            model=self._config.llm_model,
            session=self._session,
            context_hint=hint,
        )

    def _fetch_timeline(self, since_hours: int = 3) -> dict:
        if not self._config:
            return err_result(ErrorCode.PERMANENT, "抓取功能不可用，请使用 python main.py fetch 手动抓取")
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

    def _search_x_public(self, query: str, query_en: str = "", max_results: int = 20) -> dict:
        """Search X.com public square via browser (bilingual: CN + EN), save+classify, merge."""
        if not self._config:
            return err_result(ErrorCode.PERMANENT, "搜索功能不可用，浏览器未配置")
        from ..browser.browser import XBrowser
        from ..browser.timeline import TimelineScraper
        from ..db.repository import insert_tweet
        from ..db.engine import get_session as new_session
        from ..scheduler.jobs import classify_tweets_job

        queries = list(dict.fromkeys(q for q in [query, query_en] if q))
        per_query = max(5, max_results // len(queries)) if len(queries) > 1 else max_results

        browser = XBrowser(headless=True)
        try:
            browser.start()
            if not browser.load_session():
                return err_result(ErrorCode.PERMANENT, "未找到X登录会话。请运行: python main.py login")
            scraper = TimelineScraper(browser)

            all_tweets = []
            seen_ids = set()
            for q in queries:
                tweets = scraper.search(q, max_tweets=per_query)
                if tweets is None:
                    return err_result(ErrorCode.RETRYABLE, "X会话已过期。请运行: python main.py login 重新登录")
                if tweets:
                    for t in tweets:
                        if t.tweet_id not in seen_ids:
                            seen_ids.add(t.tweet_id)
                            all_tweets.append(t)

            if not all_tweets:
                return {
                    "queries": queries,
                    "total_scraped": 0,
                    "saved_to_db": 0,
                    "results": [],
                    "message": "X搜索未返回任何结果",
                }

            s = new_session()
            try:
                saved_count = 0
                for st in all_tweets:
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
                for t in all_tweets
            ]
            return {
                "queries": queries,
                "total_scraped": len(all_tweets),
                "saved_to_db": saved_count,
                "results": results,
            }
        finally:
            browser.stop()
