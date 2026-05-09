"""Scheduled job definitions — browser-based."""

import logging
import random
import time
from datetime import datetime, timezone, timedelta

from ..config import Config
from ..db.engine import get_session
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
                logger.error("fetch_timeline_job failed after retry: %s", exc)
                n = -1
    if n == 0:
        _consecutive_empty_fetches += 1
        if _consecutive_empty_fetches >= 3:
            logger.warning(
                "HEALTH: %d consecutive empty fetches — session may have expired. "
                "Run: python main.py login", _consecutive_empty_fetches)
    else:
        _consecutive_empty_fetches = 0


def classify_tweets_job(config: Config) -> None:
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
    except Exception as exc:
        logger.error("classify_tweets_job failed: %s", exc)


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
        logger.error("sync_following_job failed: %s", exc)


def send_digest_job(config: Config, period: str = "10:00") -> None:
    logger.info("Job: send_digest %s", period)
    try:
        feishu = _get_feishu(config)
        if not feishu:
            logger.warning("Feishu not configured")
            return
        session = get_session()
        try:
            now = datetime.now(timezone.utc)
            if period == "10:00":
                since = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
            else:
                since = now.replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(hours=8)
                if since > now:
                    since = since - timedelta(days=1)
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
    except Exception as exc:
        logger.error("send_digest_job failed: %s", exc)


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
        logger.error("cleanup_tweets_job failed: %s", exc)
