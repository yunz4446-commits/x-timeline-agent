"""Scheduler engine — start/stop APScheduler."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .jobs import (
    fetch_timeline_job, classify_tweets_job, send_digest_job, sync_following_job,
    cleanup_tweets_job,
)
from ..config import Config
from ..db.engine import get_session

logger = logging.getLogger(__name__)


class SchedulerRunner:
    """Wraps APScheduler lifecycle."""

    def __init__(self, config: Config):
        self._config = config
        self._scheduler = BackgroundScheduler(
            timezone=config.timezone,
            job_defaults={"misfire_grace_time": 300, "coalesce": True},
        )

    def start(self) -> None:
        cfg = self._config

        # 1) Timeline fetch: every N minutes
        self._scheduler.add_job(
            fetch_timeline_job,
            IntervalTrigger(minutes=cfg.fetch_interval_minutes),
            id="fetch_timeline",
            name="Fetch timeline",
            kwargs={"config": cfg},
        )

        # 2) Classify new tweets: every N minutes, offset by 2 min
        self._scheduler.add_job(
            classify_tweets_job,
            IntervalTrigger(minutes=cfg.fetch_interval_minutes),
            id="classify_tweets",
            name="Classify tweets",
            kwargs={"config": cfg},
        )

        # 3) Following sync: every 60 min
        self._scheduler.add_job(
            sync_following_job,
            IntervalTrigger(minutes=cfg.following_sync_interval_minutes),
            id="sync_following",
            name="Sync following",
            kwargs={"config": cfg},
        )

        # 4) Digest sends at configured times
        for t in cfg.digest_times:
            hour, minute = t.split(":")
            self._scheduler.add_job(
                send_digest_job,
                CronTrigger(hour=int(hour), minute=int(minute), timezone=cfg.timezone),
                id=f"digest_{t}",
                name=f"Send digest {t}",
                kwargs={"config": cfg, "period": t},
            )

        # 5) Cleanup old tweets: daily at 3:57 AM (off-peak)
        self._scheduler.add_job(
            cleanup_tweets_job,
            CronTrigger(hour=3, minute=57, timezone=cfg.timezone),
            id="cleanup_tweets",
            name="Cleanup old tweets",
            kwargs={"config": cfg, "months": 3},
        )

        self._scheduler.start()
        logger.info("Scheduler started with %d jobs", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
