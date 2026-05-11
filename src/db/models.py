"""SQLAlchemy ORM 模型"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean, DateTime,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class FollowedAccount(Base):
    """关注账号表 — 自动同步 X 的关注列表"""
    __tablename__ = "followed_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    twitter_user_id = Column(String(64), unique=True, nullable=False, index=True)
    username = Column(String(256), nullable=False)
    display_name = Column(String(512), default="")
    status = Column(String(16), default="active")       # active | unfollowed
    followed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    unfollowed_at = Column(DateTime, nullable=True)
    last_fetched_at = Column(DateTime, nullable=True)


class Tweet(Base):
    """推文表 — 时间线抓取的所有推文"""
    __tablename__ = "tweets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tweet_id = Column(String(64), unique=True, nullable=False, index=True)
    author_id = Column(String(64), nullable=False, index=True)
    author_username = Column(String(256), default="")
    text = Column(Text, default="")
    lang = Column(String(16), default="")
    is_retweet = Column(Boolean, default=False)
    is_reply = Column(Boolean, default=False)
    reply_to_username = Column(String(256), default="")
    like_count = Column(Integer, default=0)
    retweet_count = Column(Integer, default=0)
    reply_count = Column(Integer, default=0)
    tweet_created_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # AI 分类结果
    # 四类评分，JSON 字符串: {"discussion": 0.9, "prediction": 0.1, ...}
    category_scores = Column(Text, default="{}")
    has_link = Column(Boolean, default=False)
    link_url = Column(Text, default="")
    summary_zh = Column(Text, default="")                 # AI 一句话中文摘要

    # 是否曾在摘要中出现过
    in_digest = Column(Boolean, default=False)

    embedding = Column(Text, default="")   # JSON array of 384 floats, semantic search vector

    raw_json = Column(Text, default="")

    __table_args__ = (
        Index("idx_tweets_author_created", "author_id", "tweet_created_at"),
        Index("idx_tweets_fetched", "fetched_at"),
    )


class DigestLog(Base):
    """摘要发送日志"""
    __tablename__ = "digest_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    period = Column(String(16), nullable=False)              # "10:00" | "17:00"
    sent_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    tweet_count = Column(Integer, default=0)
    status = Column(String(16), default="success")           # success | failed
    error_message = Column(Text, default="")


class ConversationLog(Base):
    """对话历史"""
    __tablename__ = "conversation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True)
    role = Column(String(16), nullable=False)                # user | assistant
    content = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_conv_user_time", "user_id", "created_at"),
    )


class Bookmark(Base):
    """用户书签 — 标记稍后读，自存推文内容不受数据库清理影响"""
    __tablename__ = "bookmarks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False)
    tweet_id = Column(String(64), nullable=False)
    note = Column(String(512), default="")
    author_username = Column(String(256), default="")
    text = Column(Text, default="")
    link = Column(Text, default="")
    tweet_created_at = Column(DateTime, nullable=True)
    score = Column(Float, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reminded = Column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("user_id", "tweet_id", name="uq_user_tweet_bookmark"),
    )


class AgentMemory(Base):
    """长期记忆 — 纠错规则 + 话题快照"""
    __tablename__ = "agent_memories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(128), nullable=False, index=True)
    type = Column(String(32), nullable=False)   # correction | topic_snapshot
    content = Column(Text, default="")
    weight = Column(Float, default=1.0)
    extra = Column(Text, default="{}")          # JSON: {topic, decay_rate, ...}
    embedding = Column(Text, default="")        # JSON array of 384 floats
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_memory_user_type", "user_id", "type"),
    )
