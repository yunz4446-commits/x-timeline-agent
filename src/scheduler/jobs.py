"""Scheduled job definitions — browser-based."""

import logging
import random
import time
from datetime import datetime, timezone, timedelta

from ..config import Config
from ..db.engine import get_session
from ..alerter import send_alert
from ..db.repository import (
    insert_tweet, get_active_accounts,
    update_author_last_fetched, cleanup_old_tweets,
)
from ..browser.browser import XBrowser
from ..browser.timeline import TimelineScraper
from ..classifier.classify import TweetClassifier
from ..digest.builder import DigestBuilder
from ..channels.feishu import FeishuWebhook

logger = logging.getLogger(__name__)

_classifier = None
_feishu = None
_consecutive_empty_fetches = 0
_consecutive_classify_failures = 0
_consecutive_digest_failures = 0


def _get_classifier(config: Config):
    global _classifier
    if _classifier is None:
        _classifier = TweetClassifier(config.llm_api_key, config.llm_api_base, config.llm_model)
    return _classifier


def _get_feishu(config: Config):
    global _feishu
    if _feishu is None and config.feishu_webhook_url:
        _feishu = FeishuWebhook(config.feishu_webhook_url)
    return _feishu


def _scrape_timeline(config: Config, since: datetime | None = None) -> int:
    browser = XBrowser(headless=True)
    try:
        browser.start()
        if not browser.load_session():
            logger.error(
                "No X session file (data/x_session.json). "
                "Run: python main.py login")
            return 0

        scraper = TimelineScraper(browser)
        max_scrolls = config.fetch_interval_minutes * 2 + random.randint(0, 10)
        tweets = scraper.scrape(max_scrolls=max_scrolls, max_tweets=300)
        if tweets is None:
            logger.error(
                "X session expired or cookies invalid. "
                "Run: python main.py login to re-authenticate")
            return 0
        if not tweets:
            return 0

        session = get_session()
        try:
            count = 0
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
                    "raw": {
                        "display_name": st.author_display_name,
                        "link_urls": st.link_urls,
                        "views": st.view_count,
                    },
                }
                if insert_tweet(session, data):
                    count += 1
            return count
        finally:
            session.close()
    finally:
        browser.stop()


def _sync_following(config: Config, my_username: str) -> int:
    browser = XBrowser(headless=True)
    try:
        browser.start()
        if not browser.load_session():
            logger.error("No X session found")
            return 0
        scraper = TimelineScraper(browser)
        accounts = scraper.get_following_list(my_username, max_pages=5)
        if not accounts:
            return 0
        from ..db.repository import upsert_account, get_all_twitter_ids, mark_unfollowed
        session = get_session()
        try:
            remote_usernames = set()
            for acc in accounts:
                remote_usernames.add(acc["username"])
                upsert_account(session, acc["username"], acc["username"], acc.get("display_name", ""))
            local_ids = get_all_twitter_ids(session)
            removed = local_ids - remote_usernames
            for uid in removed:
                mark_unfollowed(session, uid)
            logger.info("Following sync: %d active, %d removed", len(remote_usernames), len(removed))
            return len(accounts)
        finally:
            session.close()
    finally:
        browser.stop()


def fetch_timeline_job(config: Config, since: datetime | None = None,
                       jitter: bool = True) -> None:
    global _consecutive_empty_fetches
    if jitter:
        delay = random.randint(0, 120)
        if delay > 0:
            logger.info("Job: fetch_timeline — waiting %ds jitter before start", delay)
            time.sleep(delay)
    logger.info("Job: fetch_timeline")
    for attempt in (1, 2):
        try:
            n = _scrape_timeline(config, since=since)
            logger.info("Fetched %d new tweets", n)
            break
        except Exception as exc:
            if attempt == 1:
                logger.warning("fetch_timeline_job attempt 1 failed: %s, retrying...", exc)
            else:
                logger.exception("fetch_timeline_job failed after retry")
                n = -1
    if n == 0:
        _consecutive_empty_fetches += 1
        if _consecutive_empty_fetches >= 3:
            logger.warning(
                "HEALTH: %d consecutive empty fetches — session may have expired. "
                "Run: python main.py login", _consecutive_empty_fetches)
            if config.alerts_enabled and config.feishu_webhook_url:
                send_alert(config.feishu_webhook_url,
                           "推文连续抓空",
                           f"已连续 {_consecutive_empty_fetches} 次抓取为空，X 会话可能已过期。请重新登录。",
                           level="warning")
    else:
        _consecutive_empty_fetches = 0


def classify_tweets_job(config: Config) -> None:
    global _consecutive_classify_failures
    logger.info("Job: classify_tweets")
    try:
        classifier = _get_classifier(config)
        session = get_session()
        try:
            total = 0
            while True:
                n = classifier.classify_batch(session, limit=30)
                if n == 0:
                    break
                total += n
            logger.info("Classified %d tweets", total)
        finally:
            session.close()
        _consecutive_classify_failures = 0
    except Exception as exc:
        _consecutive_classify_failures += 1
        logger.exception("classify_tweets_job failed")
        if _consecutive_classify_failures >= 3 and config.alerts_enabled and config.feishu_webhook_url:
            send_alert(config.feishu_webhook_url,
                       "LLM 分类连续失败",
                       f"classify_tweets_job 已连续失败 {_consecutive_classify_failures} 次。LLM API 可能不可用。",
                       level="error")


def sync_following_job(config: Config) -> None:
    logger.info("Job: sync_following")
    try:
        # Use the first active account's username or a config value
        session = get_session()
        try:
            accounts = get_active_accounts(session)
            if accounts:
                my_username = accounts[0].username
            else:
                my_username = "me"
        finally:
            session.close()
        n = _sync_following(config, my_username)
        logger.info("Following sync: %d accounts", n)
    except Exception as exc:
        logger.exception("sync_following_job failed")
        if config.alerts_enabled and config.feishu_webhook_url:
            send_alert(config.feishu_webhook_url,
                       "关注列表同步失败",
                       f"sync_following_job 执行异常: {exc}",
                       level="warning")


def _get_period_since(period: str, all_times: list[str], tz_name: str) -> datetime:
    """给定当前时段，返回时间窗口起点（UTC datetime）。

    period 的前一个时段为起点；如果是当天第一个时段，起点为前一天最后一个时段。
    比如 digest_times=["10:00","15:00","20:00"], period="15:00" → 返回北京时间今天 10:00(UTC)。
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    now_tz = datetime.now(tz)

    sorted_times = sorted(all_times)
    idx = sorted_times.index(period)
    if idx == 0:
        prev = sorted_times[-1]
        day_offset = -1
    else:
        prev = sorted_times[idx - 1]
        day_offset = 0
    h, m = map(int, prev.split(":"))
    since_tz = now_tz.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=day_offset)
    return since_tz.astimezone(timezone.utc)


def send_digest_job(config: Config, period: str = "10:00") -> None:
    global _consecutive_digest_failures
    logger.info("Job: send_digest %s", period)
    try:
        feishu = _get_feishu(config)
        if not feishu:
            logger.warning("Feishu not configured")
            return
        session = get_session()
        try:
            since = _get_period_since(period, config.digest_times, config.timezone)
            builder = DigestBuilder(session, since=since,
                                    min_score=config.min_interest_score,
                                    max_per=config.max_items_per_digest,
                                    llm_api_key=config.llm_api_key,
                                    llm_api_base=config.llm_api_base,
                                    llm_model=config.llm_model)
            digest = builder.build()
            if digest["total"] == 0:
                logger.info("No interesting tweets for %s", period)
                return
            md = builder.build_markdown(digest)
            feishu.send_message_sync(md)
            builder.commit(digest, period)
            logger.info("Digest %s sent: %d tweets", period, digest["total"])
        finally:
            session.close()
        _consecutive_digest_failures = 0
    except Exception as exc:
        _consecutive_digest_failures += 1
        logger.exception("send_digest_job failed")
        if _consecutive_digest_failures >= 2 and config.alerts_enabled and config.feishu_webhook_url:
            send_alert(config.feishu_webhook_url,
                       "Digest 连续发送失败",
                       f"send_digest_job 已连续失败 {_consecutive_digest_failures} 次。请检查 LLM API 和飞书连接。",
                       level="error")


def cleanup_tweets_job(config: Config, months: int = 3) -> None:
    """删除 N 个月前的推文（保留已收藏的）。"""
    logger.info("Job: cleanup_tweets (retain %d months)", months)
    try:
        session = get_session()
        try:
            deleted = cleanup_old_tweets(session, months=months)
            logger.info("Cleaned up %d old tweets (bookmarks preserved)", deleted)
        finally:
            session.close()
    except Exception as exc:
        logger.exception("cleanup_tweets_job failed")
        if config.alerts_enabled and config.feishu_webhook_url:
            send_alert(config.feishu_webhook_url,
                       "推文清理失败",
                       f"cleanup_tweets_job 执行异常: {exc}",
                       level="warning")
