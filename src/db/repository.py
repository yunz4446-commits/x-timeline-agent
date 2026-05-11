"""Data access layer."""

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .models import FollowedAccount, Tweet, DigestLog, ConversationLog, Bookmark, AgentMemory


def upsert_account(session: Session, twitter_user_id: str, username: str,
                   display_name: str = "") -> FollowedAccount:
    acc = session.query(FollowedAccount).filter_by(twitter_user_id=twitter_user_id).first()
    if acc:
        acc.username = username
        acc.display_name = display_name
        if acc.status == "unfollowed":
            acc.status = "active"
            acc.followed_at = datetime.now(timezone.utc)
            acc.unfollowed_at = None
    else:
        acc = FollowedAccount(
            twitter_user_id=twitter_user_id, username=username,
            display_name=display_name, status="active")
        session.add(acc)
    session.commit()
    return acc


def mark_unfollowed(session: Session, twitter_user_id: str) -> None:
    acc = session.query(FollowedAccount).filter_by(twitter_user_id=twitter_user_id).first()
    if acc and acc.status == "active":
        acc.status = "unfollowed"
        acc.unfollowed_at = datetime.now(timezone.utc)
        session.commit()


def get_active_accounts(session: Session) -> list:
    return session.query(FollowedAccount).filter_by(status="active").all()


def get_all_twitter_ids(session: Session) -> set:
    rows = session.query(FollowedAccount.twitter_user_id).filter_by(status="active").all()
    return {r[0] for r in rows}


def tweet_exists(session: Session, tweet_id: str) -> bool:
    return session.query(Tweet).filter_by(tweet_id=tweet_id).first() is not None


def get_tweets_by_ids(session: Session, tweet_ids: list[str]) -> list:
    return session.query(Tweet).filter(Tweet.tweet_id.in_(tweet_ids)).all()


def insert_tweet(session: Session, data: dict) -> Optional[Tweet]:
    if tweet_exists(session, data["tweet_id"]):
        return None
    tweet = Tweet(
        tweet_id=data["tweet_id"], author_id=data["author_id"],
        author_username=data.get("author_username", ""),
        text=data.get("text", ""), lang=data.get("lang", ""),
        is_retweet=data.get("is_retweet", False), is_reply=data.get("is_reply", False),
        reply_to_username=data.get("reply_to_username", ""),
        like_count=data.get("like_count", 0), retweet_count=data.get("retweet_count", 0),
        reply_count=data.get("reply_count", 0),
        tweet_created_at=data.get("tweet_created_at"),
        raw_json=json.dumps(data.get("raw", {}), ensure_ascii=False))
    session.add(tweet)
    session.commit()
    return tweet


def insert_tweets_batch(session: Session, tweets_data: list) -> int:
    count = 0
    for data in tweets_data:
        if insert_tweet(session, data):
            count += 1
    return count


def update_tweet_classification(session: Session, tweet_id: str,
                                 usefulness: float = 0,
                                 reason: str = "",
                                 summary_zh: str = "",
                                 has_link: bool = False, link_url: str = "") -> None:
    tweet = session.query(Tweet).filter_by(tweet_id=tweet_id).first()
    if tweet:
        tweet.category_scores = json.dumps(
            {"usefulness": usefulness, "reason": reason},
            ensure_ascii=False)
        tweet.summary_zh = summary_zh
        tweet.has_link = has_link
        tweet.link_url = link_url
        session.commit()


def get_unclassified_tweets(session: Session, limit: int = 50) -> list:
    return session.query(Tweet).filter(
        Tweet.category_scores == "{}").order_by(Tweet.fetched_at.desc()).limit(limit).all()


def get_tweets_since(session: Session, since: datetime,
                      min_score: float = 0.0,
                      limit: int = 200) -> list:
    """Get tweets since a given time, optionally filtered by min usefulness."""
    query = session.query(Tweet).filter(
        Tweet.tweet_created_at >= since,
        Tweet.category_scores != "{}"
    ).order_by(Tweet.tweet_created_at.desc()).limit(limit)
    if min_score <= 0:
        return query.all()
    results = query.all()
    filtered = []
    for t in results:
        try:
            scores = json.loads(t.category_scores)
        except (json.JSONDecodeError, TypeError):
            continue
        if scores.get("usefulness", 0) >= min_score:
            filtered.append(t)
    return filtered


def get_useful_tweets(session: Session, min_score: float = 0.6,
                       limit: int = 20,
                       since: datetime | None = None) -> list:
    """Get tweets with usefulness >= min_score, ordered by score desc then time desc.
    Handles both new format ({"usefulness": x}) and old format
    ({"discussion": x, ...}) by extracting usefulness directly."""
    q = session.query(Tweet).filter(
        Tweet.category_scores != "{}"
    )
    if since is not None:
        q = q.filter(
            (Tweet.tweet_created_at >= since) |
            ((Tweet.fetched_at >= since) & (Tweet.is_retweet == True))
        )
    results = q.order_by(Tweet.tweet_created_at.desc()).limit(limit * 5).all()
    scored = []
    for t in results:
        try:
            scores = json.loads(t.category_scores)
        except (json.JSONDecodeError, TypeError):
            continue
        u = scores.get("usefulness", 0)
        if u >= min_score:
            scored.append((t, u))
        if len(scored) >= limit:
            break
    scored.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scored]


def get_all_useful_tweets_since(session: Session, since: datetime,
                                min_score: float = 0.3) -> list:
    """Get ALL tweets since a cutoff, filtered by usefulness >= min_score.
    Ordered by tweet_created_at ascending (chronological for narrative)."""
    tweets = session.query(Tweet).filter(
        Tweet.tweet_created_at >= since,
        Tweet.category_scores != "{}"
    ).order_by(Tweet.tweet_created_at.asc()).all()
    filtered = []
    for t in tweets:
        try:
            scores = json.loads(t.category_scores)
        except (json.JSONDecodeError, TypeError):
            continue
        if scores.get("usefulness", 0) >= min_score:
            filtered.append(t)
    return filtered


def get_all_tweets_since(session: Session, since: datetime, limit: int | None = None) -> list:
    """获取 since 以来的全部推文（无有用度过滤，无文本过滤），按时间升序。

    用于 summarize_timeline 深度总结——需要全量阅读，不丢失任何信号。
    """
    q = session.query(Tweet).filter(
        Tweet.tweet_created_at >= since
    ).order_by(Tweet.tweet_created_at.asc())
    if limit is not None:
        q = q.limit(limit)
    return q.all()


def search_tweets_by_keyword(session: Session, keyword: str, limit: int = 50) -> list:
    """按关键词模糊搜索推文。SQL 层 LIKE，扫描全部原文和摘要，返回最近匹配的 N 条。"""
    kw = f"%{keyword}%"
    return session.query(Tweet).filter(
        (Tweet.text.ilike(kw)) | (Tweet.summary_zh.ilike(kw))
    ).order_by(Tweet.tweet_created_at.desc()).limit(limit).all()


def get_tweets_needing_embedding(session: Session, limit: int = 100) -> list:
    """获取有原文但尚未生成 embedding 的推文"""
    return session.query(Tweet).filter(
        Tweet.embedding == "",
        Tweet.text != ""
    ).limit(limit).all()


def update_tweet_embedding(session: Session, tweet_id: str,
                            embedding: str) -> None:
    tweet = session.query(Tweet).filter_by(tweet_id=tweet_id).first()
    if tweet:
        tweet.embedding = embedding
        session.commit()


def semantic_search(session: Session, query: str, top_k: int = 20,
                    days: int = 7, max_candidates: int = 1500) -> list:
    """向量语义搜索推文。

    拉取近期已嵌入推文作为候选集，将 query 转为 embedding，
    对候选集做余弦相似度排序，返回 top_k 条。
    """
    import json
    from datetime import datetime, timezone, timedelta
    from ..search.embedding import search as vector_search

    since = datetime.now(timezone.utc) - timedelta(days=days)
    candidates = session.query(Tweet).filter(
        Tweet.embedding != "",
        Tweet.tweet_created_at >= since
    ).order_by(Tweet.tweet_created_at.desc()).limit(max_candidates).all()

    if not candidates:
        return []

    candidate_dicts = []
    for t in candidates:
        try:
            json.loads(t.embedding)
        except (json.JSONDecodeError, TypeError):
            continue
        candidate_dicts.append({
            "tweet_id": t.tweet_id,
            "author_username": t.author_username,
            "text": t.text or "",
            "summary_zh": t.summary_zh or "",
            "tweet_created_at": t.tweet_created_at,
            "has_link": t.has_link,
            "link_url": t.link_url,
        })

    if not candidate_dicts:
        return []

    return vector_search(query, candidate_dicts, top_k=top_k)


def get_trending_topics(session: Session, hours: int = 24, top_n: int = 10) -> list:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    tweets = session.query(Tweet).filter(
        Tweet.tweet_created_at >= since,
        Tweet.category_scores != "{}").order_by(
            Tweet.tweet_created_at.desc()).limit(200).all()
    scored = []
    for t in tweets:
        try:
            scores = json.loads(t.category_scores)
        except (json.JSONDecodeError, TypeError):
            continue
        usefulness = scores.get("usefulness", 0)
        if usefulness >= 0.5:
            weight = usefulness * 0.6 + (t.like_count + t.retweet_count * 2) * 0.0001
            scored.append((t, weight))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scored[:top_n]]


def get_author_latest_tweet_id(session: Session, author_id: str) -> Optional[str]:
    row = session.query(Tweet.tweet_id).filter_by(author_id=author_id).order_by(
        Tweet.tweet_created_at.desc()).first()
    return row[0] if row else None


def update_author_last_fetched(session: Session, author_id: str) -> None:
    acc = session.query(FollowedAccount).filter_by(twitter_user_id=author_id).first()
    if acc:
        acc.last_fetched_at = datetime.now(timezone.utc)
        session.commit()


def mark_tweet_in_digest(session: Session, tweet_ids: list) -> None:
    session.query(Tweet).filter(Tweet.tweet_id.in_(tweet_ids)).update(
        {Tweet.in_digest: True}, synchronize_session=False)
    session.commit()


def log_digest(session: Session, period: str, tweet_count: int,
               status: str = "success", error: str = "") -> DigestLog:
    log = DigestLog(period=period, tweet_count=tweet_count, status=status, error_message=error)
    session.add(log)
    session.commit()
    return log


def save_conversation(session: Session, user_id: str, role: str, content: str) -> None:
    conv = ConversationLog(user_id=user_id, role=role, content=content)
    session.add(conv)
    session.commit()


def get_recent_conversations(session: Session, user_id: str, limit: int = 20) -> list:
    return session.query(ConversationLog).filter_by(user_id=user_id).order_by(
        ConversationLog.created_at.desc()).limit(limit).all()[::-1]


def add_bookmark(session: Session, user_id: str, tweet_id: str,
                 note: str = "", author_username: str = "",
                 text: str = "", link: str = "",
                 tweet_created_at=None, score: float = 0.0) -> Bookmark:
    existing = session.query(Bookmark).filter_by(user_id=user_id, tweet_id=tweet_id).first()
    if existing:
        existing.note = note or existing.note
        if author_username:
            existing.author_username = author_username
        if text:
            existing.text = text
        if link:
            existing.link = link
        if tweet_created_at:
            existing.tweet_created_at = tweet_created_at
        if score:
            existing.score = score
        session.commit()
        return existing
    bm = Bookmark(
        user_id=user_id, tweet_id=tweet_id, note=note,
        author_username=author_username, text=text, link=link,
        tweet_created_at=tweet_created_at, score=score)
    session.add(bm)
    session.commit()
    return bm


def get_bookmarks(session: Session, user_id: str) -> list:
    return session.query(Bookmark).filter_by(user_id=user_id).order_by(
        Bookmark.created_at.desc()).all()


def delete_bookmark(session: Session, user_id: str, tweet_id: str) -> bool:
    bm = session.query(Bookmark).filter_by(user_id=user_id, tweet_id=tweet_id).first()
    if bm:
        session.delete(bm)
        session.commit()
        return True
    return False


def get_unreminded_bookmarks(session: Session, user_id: str) -> list:
    return session.query(Bookmark).filter_by(user_id=user_id, reminded=False).all()


def mark_bookmark_reminded(session: Session, bookmark_id: int) -> None:
    bm = session.query(Bookmark).get(bookmark_id)
    if bm:
        bm.reminded = True
        session.commit()


def get_task_archives(session: Session, user_id: str, limit: int = 5) -> list:
    return session.query(ConversationLog).filter_by(
        user_id=user_id, role="task_archive"
    ).order_by(ConversationLog.created_at.desc()).limit(limit).all()


def delete_old_task_archives(session: Session, user_id: str, keep: int = 5) -> int:
    """清理旧归档，保留最近 keep 条，返回删除数。"""
    all_ids = [r[0] for r in session.query(ConversationLog.id).filter_by(
        user_id=user_id, role="task_archive"
    ).order_by(ConversationLog.created_at.desc()).all()]
    if len(all_ids) <= keep:
        return 0
    to_delete = all_ids[keep:]
    deleted = session.query(ConversationLog).filter(
        ConversationLog.id.in_(to_delete)
    ).delete(synchronize_session=False)
    session.commit()
    return deleted


# ── 长期记忆 ──

def get_corrections(session: Session, user_id: str) -> list:
    return session.query(AgentMemory).filter_by(
        user_id=user_id, type="correction"
    ).order_by(AgentMemory.updated_at.desc()).all()


def get_latest_snapshot(session: Session, user_id: str) -> AgentMemory | None:
    return session.query(AgentMemory).filter_by(
        user_id=user_id, type="topic_snapshot"
    ).order_by(AgentMemory.created_at.desc()).first()


def save_memory(session: Session, user_id: str, mtype: str, content: str,
                embedding: str = "", extra: dict | None = None) -> AgentMemory:
    mem = AgentMemory(
        user_id=user_id, type=mtype, content=content,
        embedding=embedding, extra=json.dumps(extra or {}, ensure_ascii=False))
    session.add(mem)
    session.commit()
    return mem


def update_memory(session: Session, memory_id: int, content: str,
                  embedding: str = "", extra: dict | None = None) -> None:
    mem = session.query(AgentMemory).get(memory_id)
    if mem:
        mem.content = content
        mem.weight = 1.0
        mem.updated_at = datetime.now(timezone.utc)
        if embedding:
            mem.embedding = embedding
        if extra:
            mem.extra = json.dumps(extra, ensure_ascii=False)
        session.commit()


def search_similar_memory(session: Session, user_id: str, mtype: str,
                          embedding: str, threshold: float = 0.7) -> AgentMemory | None:
    """用余弦相似度找已有相似记忆，用于合并去重。"""
    if not embedding:
        return None
    candidates = session.query(AgentMemory).filter_by(
        user_id=user_id, type=mtype
    ).filter(AgentMemory.embedding != "").all()
    if not candidates:
        return None
    try:
        import numpy as np
        query_vec = np.array(json.loads(embedding))
        best, best_score = None, 0
        for c in candidates:
            c_vec = np.array(json.loads(c.embedding))
            score = np.dot(query_vec, c_vec)
            if score > best_score:
                best_score = score
                best = c
        if best and best_score >= threshold:
            return best
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def cleanup_snapshots(session: Session, user_id: str, max_per_topic: int = 10) -> int:
    """同一 topic 的快照超过上限时删最旧。"""
    deleted = 0
    snapshots = session.query(AgentMemory).filter_by(
        user_id=user_id, type="topic_snapshot"
    ).order_by(AgentMemory.created_at.desc()).all()
    by_topic: dict[str, list] = {}
    for s in snapshots:
        try:
            topic = json.loads(s.extra).get("topic", "_")
        except (json.JSONDecodeError, TypeError):
            topic = "_"
        by_topic.setdefault(topic, []).append(s)
    for topic, items in by_topic.items():
        if len(items) > max_per_topic:
            for s in items[max_per_topic:]:
                session.delete(s)
                deleted += 1
    if deleted:
        session.commit()
    return deleted


def list_memories(session: Session, user_id: str) -> list:
    return session.query(AgentMemory).filter_by(user_id=user_id).order_by(
        AgentMemory.updated_at.desc()).all()


def delete_memory(session: Session, memory_id: int) -> bool:
    mem = session.query(AgentMemory).get(memory_id)
    if mem:
        session.delete(mem)
        session.commit()
        return True
    return False


def clear_memories_by_type(session: Session, user_id: str, mtype: str) -> int:
    deleted = session.query(AgentMemory).filter_by(
        user_id=user_id, type=mtype).delete()
    session.commit()
    return deleted


def cleanup_old_tweets(session: Session, months: int = 3) -> int:
    """删除 N 个月前的推文，但保留已被收藏的。

    Returns:
        实际删除的推文数量
    """
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)

    bookmarked = {row[0] for row in session.query(Bookmark.tweet_id).all()}

    if bookmarked:
        deleted = session.query(Tweet).filter(
            Tweet.tweet_created_at < cutoff,
            ~Tweet.tweet_id.in_(bookmarked),
        ).delete(synchronize_session=False)
    else:
        deleted = session.query(Tweet).filter(
            Tweet.tweet_created_at < cutoff,
        ).delete(synchronize_session=False)

    session.commit()
    return deleted
