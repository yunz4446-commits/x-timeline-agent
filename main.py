#!/usr/bin/env python
"""X Timeline Agent — main entry point."""

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config
from src.db.engine import init_db


def setup_logging():
    from src.logging_setup import setup_logging as do_setup
    from src.config import load_config as _lc
    cfg = _lc()
    do_setup(
        log_level=getattr(cfg, "log_level", None) or "INFO",
        log_dir=getattr(cfg, "log_dir", None) or "logs",
    )


def cmd_run(config):
    """Start the background scheduler + Feishu callback server."""
    import threading
    from src.channels.feishu_server import create_app

    # Start Flask in daemon thread for Feishu callbacks
    app = create_app(config)
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=8080, debug=False),
        daemon=True,
    )
    t.start()
    print("[Agent] Feishu callback server started on :8080")

    from src.scheduler.engine import SchedulerRunner
    runner = SchedulerRunner(config)
    runner.start()
    print("[Agent] Scheduler running. Press Ctrl+C to stop.")
    try:
        signal.pause()
    except (KeyboardInterrupt, AttributeError):
        pass
    runner.stop()
    print("[Agent] Stopped.")


def cmd_fetch(config, since_str: str = ""):
    """Manual one-shot fetch + classify."""
    from datetime import datetime, timezone
    from src.scheduler.jobs import fetch_timeline_job, classify_tweets_job
    from dateutil.parser import isoparse as parse_dt
    since = None
    if since_str:
        try:
            # Try ISO format first: "2026-05-03T17:00:00"
            since = parse_dt(since_str)
        except Exception:
            # Try time-only like "17:00" — use today
            try:
                h, m = map(int, since_str.split(":"))
                since = datetime.now(timezone.utc).replace(
                    hour=h, minute=m, second=0, microsecond=0)
                # Convert to Asia/Shanghai (UTC+8)
                from datetime import timedelta
                since = since - timedelta(hours=8)
            except Exception:
                print(f"[WARN] Cannot parse --since '{since_str}', using default 6h")
    fetch_timeline_job(config, since=since, jitter=False)
    for _ in range(3):
        classify_tweets_job(config)
    print("[Agent] Fetch + classify complete.")


def cmd_digest(config):
    """Manual one-shot digest send."""
    from src.scheduler.jobs import send_digest_job
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    sorted_times = sorted(config.digest_times)
    # 找到当天第一个尚未到达的时段，若全部已过则用第一个（次日）
    period = sorted_times[0]
    for t in sorted_times:
        t_hour = int(t.split(":")[0])
        if now.hour < t_hour:
            period = t
            break
    send_digest_job(config, period)
    print(f"[Agent] Digest ({period}) sent.")


def cmd_sync(config):
    """Manual following sync."""
    from src.scheduler.jobs import sync_following_job
    sync_following_job(config)
    print("[Agent] Following sync complete.")


def cmd_status(config):
    """Print system status."""
    from src.db.engine import get_session
    from src.db.models import Tweet, FollowedAccount, DigestLog
    session = get_session()
    try:
        total_tweets = session.query(Tweet).count()
        classified = session.query(Tweet).filter(Tweet.category_scores != "{}").count()
        active_accounts = session.query(FollowedAccount).filter_by(status="active").count()
        total_accounts = session.query(FollowedAccount).count()
        last_digest = session.query(DigestLog).order_by(DigestLog.sent_at.desc()).first()
        print(f"Total tweets: {total_tweets}")
        print(f"Classified: {classified} ({classified*100//max(total_tweets,1)}%)")
        print(f"Active followed accounts: {active_accounts}")
        print(f"Total accounts seen: {total_accounts}")
        if last_digest:
            print(f"Last digest: {last_digest.sent_at} ({last_digest.period}, {last_digest.tweet_count} tweets)")
        else:
            print("No digest sent yet")
    finally:
        session.close()


def cmd_login(config):
    """Open browser to log into X.com and save session."""
    from src.browser.browser import XBrowser
    browser = XBrowser(headless=False)
    try:
        browser.start()
        if browser.load_session() and browser.is_logged_in():
            print('[Login] Already logged in! Session is valid.')
            return
        print('[Login] Opening X.com login page...')
        ok = browser.login()
        if ok:
            print('[Login] Session saved. You can now run: python main.py run')
        else:
            print('[Login] Login failed or timed out. Try again.')
    finally:
        browser.stop()


def cmd_cleanup(config, months: int = 3):
    """手动清理 N 个月前的旧推文（保留已收藏的）。"""
    from src.scheduler.jobs import cleanup_tweets_job
    cleanup_tweets_job(config, months=months)
    print(f"[Agent] Cleanup ({months} months) complete.")


def cmd_chat(config):
    """Interactive chat with the timeline agent."""
    from src.agent.core import TimelineAgent
    agent = TimelineAgent(
        api_key=config.llm_api_key,
        api_base=config.llm_api_base,
        model=config.llm_model,
        config=config,
    )
    print("=" * 50)
    print("X Timeline Agent — 交互对话")
    print("输入 'quit' 或 'exit' 退出")
    print("试试问我: '今天有什么教程？' / '最近在聊什么？' / '帮我刷新一下'")
    print("=" * 50)
    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见!")
            break
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("再见!")
            break
        print("思考中...", end="\r")
        response = agent.handle_message(user_input)
        print("\033[K" + response)


def cmd_setup(config):
    """First-run setup wizard."""
    print("=" * 50)
    print("X Timeline Agent - Setup")
    print("=" * 50)
    issues = []
    if not config.llm_api_key:
        issues.append("[MISSING] LLM API Key (set LLM_API_KEY in .env)")
    if not config.feishu_webhook_url:
        issues.append("[WARNING] Feishu Webhook not set — you won't receive digests (set FEISHU_WEBHOOK_URL in .env)")
    if issues:
        for i in issues:
            print(f"  {i}")
        print("\nCopy .env.example to .env and fill in the values:")
        print("  cp .env.example .env")
    else:
        print("All required settings found!")
        print(f"  LLM: {config.llm_provider}/{config.llm_model}")
        print(f"  Feishu webhook: {'configured' if config.feishu_webhook_url else 'not set'}")
        # Check X login status via browser session
        import json
        session_file = "data/x_session.json"
        if os.path.exists(session_file):
            try:
                with open(session_file) as f:
                    data = json.load(f)
                has_auth = any(c.get("name") == "auth_token" for c in data) if isinstance(data, list) else False
                if has_auth:
                    print(f"  X session: found (re-login via: python main.py login)")
                else:
                    print(f"  X session: no auth_token — run: python main.py login")
            except Exception:
                print(f"  X session: invalid file — run: python main.py login")
        else:
            print(f"  X session: not found — run: python main.py login")
        print("\nNext steps:")
        print("  1) python main.py login     (authenticate X)")
        print("  2) python main.py fetch     (test scraping)")
        print("  3) python main.py run       (start scheduler)")
        print("  4) python main.py chat      (interactive mode)")


COMMANDS = {
    "run": cmd_run,
    "fetch": cmd_fetch,
    "digest": cmd_digest,
    "sync": cmd_sync,
    "cleanup": cmd_cleanup,
    "status": cmd_status,
    "login": cmd_login,
    "chat": cmd_chat,
    "setup": cmd_setup,
}


def main():
    setup_logging()
    parser = argparse.ArgumentParser(prog="xtimeline", description="X Timeline Agent")
    parser.add_argument("command", nargs="?", default="run",
                        choices=list(COMMANDS),
                        help="Command to run")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--since", default="", help="Time window start (e.g. '17:00' or ISO datetime)")
    parser.add_argument("--months", type=int, default=3, help="Months to retain for cleanup")
    args = parser.parse_args()

    config = load_config(args.config)
    # Ensure data directory exists
    os.makedirs("data", exist_ok=True)
    init_db(config.db_path)

    cmd_fn = COMMANDS[args.command]
    if args.command == "fetch":
        cmd_fn(config, since_str=args.since)
    elif args.command == "cleanup":
        cmd_fn(config, months=args.months)
    else:
        cmd_fn(config)


if __name__ == "__main__":
    main()
