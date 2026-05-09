"""Flask HTTP server — receives Feishu bot callbacks."""

import json
import logging
import threading

from flask import Flask, request, jsonify

logger = logging.getLogger(__name__)

_agent = None
_bot = None
_config = None


def create_app(config):
    """Create Flask app, wire config and global singletons."""
    global _config, _bot, _agent
    _config = config

    if config.feishu_app_id and config.feishu_app_secret:
        from .feishu_bot import FeishuBot
        _bot = FeishuBot(config.feishu_app_id, config.feishu_app_secret)

    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    @app.route("/feishu/callback", methods=["POST"])
    def callback():
        body = request.get_json(force=True, silent=True) or {}

        # URL verification
        if body.get("type") == "url_verification":
            challenge = body.get("challenge", "")
            logger.info("Feishu URL verification challenge: %s", challenge)
            return jsonify({"challenge": challenge})

        # Event callback
        logger.info("Feishu callback received: %s", json.dumps(body, ensure_ascii=False)[:500])
        header = body.get("header", {})
        event_type = header.get("event_type", "")
        if event_type != "im.message.receive_v1":
            logger.info("Ignored event type: %s", event_type)
            return jsonify({})

        event = body.get("event", {})
        message = event.get("message", {})
        if not message:
            return jsonify({})

        # Only process text messages
        msg_type = message.get("message_type", "")
        if msg_type != "text":
            logger.debug("Ignored msg_type: %s", msg_type)
            return jsonify({})

        content_str = message.get("content", "{}")
        try:
            content = json.loads(content_str)
        except (json.JSONDecodeError, TypeError):
            return jsonify({})

        text = (content.get("text", "") or "").strip()
        if not text:
            return jsonify({})

        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        open_id = sender_id.get("open_id", "")

        logger.info("Feishu message from %s: %s", open_id, text)

        # Process in background (Feishu requires 200 within 3s)
        threading.Thread(
            target=_handle_message,
            args=(text, open_id),
            daemon=True,
        ).start()

        return jsonify({})

    return app


def _handle_message(text: str, open_id: str) -> None:
    try:
        global _agent, _bot
        logger.info("Handling from %s: %s", open_id, text[:80])
        if _bot is None:
            logger.error("FeishuBot not initialized — check FEISHU_APP_ID and FEISHU_APP_SECRET in .env")
            return
        if not open_id:
            logger.error("No open_id in message event")
            return
        if _agent is None:
            from ..agent.core import TimelineAgent
            _agent = TimelineAgent(
                api_key=_config.llm_api_key,
                api_base=_config.llm_api_base,
                model=_config.llm_model,
                config=_config,
            )
        response = _agent.handle_message(text, user_id=open_id)
        logger.info("Reply to %s: %s", open_id, response[:80])
        _bot.send_message_sync(response, open_id)
    except Exception as exc:
        logger.error("Feishu message handler error: %s", exc)
        if _bot and open_id:
            _bot.send_message_sync(f"(出错了: {exc})", open_id)
