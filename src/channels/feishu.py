"""Feishu channel — webhook push + app bot."""

import json
import logging
from datetime import datetime

import requests

from .base import Channel

logger = logging.getLogger(__name__)


class FeishuWebhook(Channel):
    """Send messages via Feishu incoming webhook (simplest integration)."""

    def __init__(self, webhook_url: str):
        self._url = webhook_url

    async def send_message(self, content: str, chat_id: str | None = None) -> bool:
        return self._post({
            "msg_type": "interactive",
            "card": self._build_markdown_card(content),
        })

    async def send_card(self, card: dict, chat_id: str | None = None) -> bool:
        return self._post({
            "msg_type": "interactive",
            "card": card,
        })

    def send_message_sync(self, content: str) -> bool:
        """Synchronous version for use in scheduler jobs."""
        return self._post({
            "msg_type": "interactive",
            "card": self._build_markdown_card(content),
        })

    def send_card_sync(self, card: dict) -> bool:
        return self._post({"msg_type": "interactive", "card": card})

    def _build_markdown_card(self, content: str) -> dict:
        return {
            "header": {
                "title": {"tag": "plain_text", "content": "X Timeline Digest"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": content},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text",
                         "content": f"Generated at {datetime.now().strftime('%Y-%m-%d %H:%M')}"}
                    ],
                },
            ],
        }

    def _post(self, body: dict) -> bool:
        try:
            r = requests.post(self._url, json=body, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                logger.error("Feishu error: %s", data)
                return False
            return True
        except Exception as exc:
            logger.error("Feishu webhook failed: %s", exc)
            return False


from .feishu_bot import FeishuBot  # noqa: F401 — real implementation
