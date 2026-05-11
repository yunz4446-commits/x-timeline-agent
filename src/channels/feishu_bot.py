"""Feishu enterprise bot — tenant access token + message sending."""

import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{API_BASE}/auth/v3/tenant_access_token/internal"
SEND_URL = f"{API_BASE}/im/v1/messages?receive_id_type=open_id"


class FeishuBot:
    """Full bidirectional bot via Feishu App."""

    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------
    def _fetch_token(self) -> str:
        resp = requests.post(
            TOKEN_URL,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu token failed: {data}")
        self._token = data["tenant_access_token"]
        self._token_expires_at = time.time() + data.get("expire", 3600) - 120
        logger.info("FeishuBot: token obtained")
        return self._token

    def get_token(self) -> str:
        if self._token is None or time.time() >= self._token_expires_at:
            return self._fetch_token()
        return self._token

    # ------------------------------------------------------------------
    # Send message
    # ------------------------------------------------------------------
    def send_message_sync(self, content: str, open_id: str) -> bool:
        """Send a text-like card to a user by open_id."""
        card = _build_text_card(content)
        return self._send({
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        })

    def send_card_sync(self, card: dict, open_id: str) -> bool:
        return self._send({
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        })

    def _send(self, body: dict) -> bool:
        try:
            token = self.get_token()
            resp = requests.post(
                SEND_URL,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.error("FeishuBot send failed: %s", data)
                return False
            return True
        except Exception as exc:
            logger.exception("FeishuBot send error")
            return False


def _build_text_card(text: str) -> dict:
    return {
        "header": {
            "title": {"tag": "plain_text", "content": "X Timeline"},
            "template": "blue",
        },
        "elements": [
            {"tag": "markdown", "content": text},
        ],
    }
