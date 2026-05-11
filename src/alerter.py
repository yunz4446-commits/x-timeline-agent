"""飞书告警推送 + 防抖。"""

import logging
import time
import json

import requests

logger = logging.getLogger(__name__)

_cooldowns: dict[str, float] = {}  # key → last_sent_ts


def _should_send(key: str, cooldown_seconds: int = 3600) -> bool:
    now = time.time()
    last = _cooldowns.get(key, 0)
    if now - last < cooldown_seconds:
        return False
    _cooldowns[key] = now
    return True


def send_alert(webhook_url: str, title: str, content: str,
               level: str = "warning") -> bool:
    """发送飞书告警卡片。相同 title 1 小时内不重复。"""
    if not webhook_url:
        return False
    if not _should_send(title):
        return False

    color_map = {"info": "blue", "warning": "yellow", "error": "red"}
    body = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"content": f"[{level.upper()}] {title}", "tag": "plain_text"},
                "template": color_map.get(level, "yellow"),
            },
            "elements": [
                {"tag": "div", "text": {"content": content, "tag": "plain_text"}},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": f"时间: {time.strftime('%Y-%m-%d %H:%M')}"}
                ]},
            ],
        },
    }
    try:
        r = requests.post(webhook_url, json=body, timeout=10)
        if r.status_code == 200:
            logger.info("Alert sent: %s", title)
        else:
            logger.warning("Alert failed: %s → %s", title, r.text[:100])
    except Exception:
        logger.warning("Alert send exception: %s", title)
    return True
