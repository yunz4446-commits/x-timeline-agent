"""AI content classifier — single usefulness score per tweet."""

import json
import logging
from typing import Optional

from openai import OpenAI
from sqlalchemy.orm import Session

from ..db.repository import get_unclassified_tweets, update_tweet_classification

logger = logging.getLogger(__name__)

BATCH_PROMPT = """You are scoring tweets for a user who trades crypto and follows the AI industry.
The user follows these accounts intentionally — they are the user's curated information sources.
Content from these accounts is inherently more relevant than content from strangers.

A tweet is "useful" (0.0-1.0) if it provides value in ANY of these dimensions:

1. 信息差/Alpha — on-chain data, early opportunities, anomalies others missed
2. 可操作的判断 — actionable analysis with reasoning, any length
3. 市场情绪 — crowd fear/greed, what most people are talking about, market vibe
4. 实盘/仓位分享 — sharing personal positions, entry/exit, P&L, trade journals, rebalancing, "I bought/sold X", "my portfilio is...", trade reflections and lessons learned. This applies to ANY account the user follows, not just big names
5. 关键事件 — policy changes, hacks, liquidations, protocol upgrades, catalysts
6. AI实用信息 — new tools, new models, actionable technical details
7. Meme声量 — meme coin shilling density, accounts shouting a token ticker

Scoring guide:
- 0.0-0.2: noise, GM/WAGMI, spam, ads, giveaways, pure engagement farming
- 0.3-0.5: mildly interesting but no real signal (vague commentary, reposted news)
- 0.6-0.7: solid signal worth reading — clear opinion, useful info, notable event. Personal position/trade sharing from followed accounts starts here
- 0.8-0.9: strong signal — conviction call, alpha leak, actionable insight, detailed trade breakdown with reasoning
- 1.0: must-read, direct PnL impact, urgent actionable intelligence

IMPORTANT: Personal trading content (dimension 4) from accounts the user follows is VALUABLE.
A tweet like "加了点SOL仓位" or "止损了BTC空单" is at least 0.6, even if short or from a small account.
The user curated these follows precisely to see this kind of content.

A tweet only needs to score high on ONE dimension to be useful.
Short tweets can be highly useful if they convey conviction or position info.

Return a JSON array with one object per tweet (same order):
[{{
  "id": tweet_number,
  "usefulness": 0.0,
  "summary_zh": "one-line Chinese summary under 30 chars",
  "reason": "short label: which dimension(s) and why, under 20 chars",
  "has_link": true/false,
  "link_url": "extracted url or empty"
}}]

Return ONLY the JSON array, no markdown, no extra text.

Tweets:
{tweet_list}"""

MIN_SCORE = 0.5
BATCH_SIZE = 30


class TweetClassifier:
    """Classify tweets via LLM into the 4 content categories."""

    def __init__(self, api_key: str, api_base: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat"):
        self._client = OpenAI(api_key=api_key, base_url=api_base, timeout=90.0, max_retries=2)
        self._model = model

    def classify_batch(self, session: Session, limit: int = BATCH_SIZE) -> int:
        """Classify unclassified tweets in a single batch API call. Returns count."""
        tweets = get_unclassified_tweets(session, limit=limit)
        if not tweets:
            return 0

        # Build batch prompt
        lines = []
        for i, t in enumerate(tweets, 1):
            text = (t.text or "")[:800]
            link = t.link_url or ""
            lines.append(f"[{i}] text: {text}")
            if link:
                lines.append(f"    link: {link}")
        prompt = BATCH_PROMPT.format(tweet_list=chr(10).join(lines))

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=4000,
            )
            content = resp.choices[0].message.content or "[]"
            content = content.strip()
            # Strip markdown fences
            if "```" in content:
                content = content.replace("```json", "").replace("```", "").strip()
            # Extract JSON — find first [ or {
            start = -1
            for i, c in enumerate(content):
                if c in ('[', '{'):
                    start = i
                    break
            if start >= 0:
                content = content[start:]
            results = json.loads(content)
            if isinstance(results, dict):
                # Sometimes LLM wraps in {"tweets": [...]} or returns keyed object
                for val in results.values():
                    if isinstance(val, list):
                        results = val
                        break
                else:
                    logger.error("Unexpected batch result format: %s", str(results)[:200])
                    return 0
        except json.JSONDecodeError as exc:
            logger.error("Batch classify JSON parse failed: %s", exc)
            logger.debug("Raw content: %s", content[:500])
            return 0
        except Exception as exc:
            logger.error("Batch classify API failed: %s", exc)
            return 0

        # Update tweets
        tweet_map = {i + 1: t for i, t in enumerate(tweets)}
        updated = 0
        for item in results:
            tid = int(item.get("id", 0))
            tweet = tweet_map.get(tid)
            if not tweet:
                continue
            try:
                usefulness = float(item.get("usefulness", 0))
                reason = str(item.get("reason", ""))
                has_link = item.get("has_link", False)
                if isinstance(has_link, str):
                    has_link = has_link.lower() == "true"
                update_tweet_classification(
                    session, tweet.tweet_id,
                    usefulness=usefulness,
                    reason=reason,
                    summary_zh=str(item.get("summary_zh", "")),
                    has_link=has_link,
                    link_url=str(item.get("link_url", "")),
                )
                updated += 1
            except Exception as exc:
                logger.warning("Update failed for %s: %s", tweet.tweet_id, exc)
                update_tweet_classification(
                    session, tweet.tweet_id,
                    usefulness=0, reason="",
                    summary_zh="", has_link=False, link_url="",
                )

        logger.info("Classified %d/%d tweets (batch)", updated, len(tweets))

        # Generate embeddings for later semantic search
        self._embed_batch(session, tweets, tweet_map, results)

        return updated

    @staticmethod
    def _embed_batch(session, tweets, tweet_map, results):
        """分类后为推文生成 embedding 向量，供语义搜索使用。"""
        import json
        from ..db.repository import update_tweet_embedding
        from ..search.embedding import encode

        texts_to_embed = []
        ids_to_embed = []
        for item in results:
            tid = int(item.get("id", 0))
            tweet = tweet_map.get(tid)
            if tweet and tweet.text and tweet.text.strip():
                texts_to_embed.append(tweet.text[:500])
                ids_to_embed.append(tweet.tweet_id)

        if not texts_to_embed:
            return

        try:
            embeddings = encode(texts_to_embed)
        except Exception as exc:
            logger.warning("Embedding generation failed: %s", exc)
            return

        for tweet_id, emb in zip(ids_to_embed, embeddings):
            try:
                update_tweet_embedding(
                    session, tweet_id,
                    json.dumps(emb.tolist()))
            except Exception as exc:
                logger.warning(
                    "Embedding save failed for %s: %s", tweet_id, exc)

        logger.info("Embedded %d tweets", len(ids_to_embed))

    def classify_text(self, text: str) -> dict:
        """Classify a single text (used for ad-hoc classification)."""
        prompt = BATCH_PROMPT.format(
            tweet_list=f"[1] text: {text[:1000]}")
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        content = resp.choices[0].message.content or "{}"
        content = content.strip()
        if content.startswith("```"):
            content = content.split(chr(10), 1)[-1]
            if content.endswith("```"):
                content = content[:-3]
        try:
            results = json.loads(content)
            return results[0] if isinstance(results, list) else results
        except (json.JSONDecodeError, IndexError):
            return {"usefulness": 0, "summary_zh": "", "reason": "", "has_link": False, "link_url": ""}
