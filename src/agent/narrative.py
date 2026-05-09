"""Narrative manager — persistent market memo that accumulates across queries."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

NARRATIVE_FILE = "data/narrative.json"

UPDATE_PROMPT = """你是加密货币+AI市场观察者。你维护一份持续更新的市场备忘录。

## 旧备忘录
{narrative_context}

## 新推文
（每条: @作者 时间 摘要，按时间升序）
{tweet_texts}

## 任务
基于旧备忘录和新推文，更新市场备忘录：

1. 继承旧备忘录中仍然有效的洞察和趋势
2. 融入新推文中的新主题、新事件、新观点——具体到币种、项目名、人名
3. 如果旧主题在新推文中继续出现，描述是"延续"还是"变化"
4. 如果同一主题出现相反观点，标注分歧（谁看多 vs 谁看空）
5. 如果有 meme 币喊单、KOL 集体转向等群体行为，指出
6. 更新整体市场情绪

返回纯 JSON（不要 markdown）：
{{"narrative": "更新后的梗概（3-5句中文，具体到币种/事件/人名）",
 "themes": ["主题1", "主题2", "..."],
 "sentiment": "偏多/偏空/分歧/观望"}}"""


FIRST_PROMPT = """你是加密货币+AI市场观察者。请基于以下推文生成一份市场备忘录。

## 推文
（每条: @作者 时间 摘要，按时间升序）
{tweet_texts}

## 任务
分析这些推文，生成一份梗概：

1. 大家主要在聊什么？具体到币种、项目名、人名
2. 有没有反复出现的主题、KOL 集体表态、meme 币喊单？
3. 不同观点有没有分歧？
4. 整体市场情绪是什么？

返回纯 JSON（不要 markdown）：
{{"narrative": "梗概（3-5句中文，具体到币种/事件/人名）",
 "themes": ["主题1", "主题2", "..."],
 "sentiment": "偏多/偏空/分歧/观望"}}"""


class NarrativeManager:

    def __init__(self, filepath: str = NARRATIVE_FILE):
        self._filepath = filepath

    def load(self) -> dict | None:
        """Load persisted narrative state. Returns None if no file."""
        try:
            return json.loads(Path(self._filepath).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def save(self, narrative: str, themes: list, sentiment: str,
             last_tweet_created_at: str) -> None:
        """Persist narrative state."""
        Path(self._filepath).parent.mkdir(parents=True, exist_ok=True)
        state = {
            "narrative": narrative,
            "themes": themes,
            "sentiment": sentiment,
            "last_tweet_created_at": last_tweet_created_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        Path(self._filepath).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def update(self, llm_client, model: str,
               old_state: dict | None,
               new_tweets: list) -> dict | None:
        """Update narrative via LLM. Returns {narrative, themes, sentiment} or None."""
        texts = []
        for t in new_tweets[:80]:
            ts = ""
            if t.tweet_created_at:
                ts = t.tweet_created_at.strftime("%H:%M")
            summary = (t.summary_zh or (t.text or "")[:100])
            texts.append(f"@{t.author_username} {ts} {summary}")

        if old_state:
            ctx = f"narrative: {old_state.get('narrative', '')}\n"
            ctx += f"themes: {', '.join(old_state.get('themes', []))}\n"
            ctx += f"sentiment: {old_state.get('sentiment', '')}"
            prompt = UPDATE_PROMPT.format(
                narrative_context=ctx,
                tweet_texts=chr(10).join(texts))
        else:
            prompt = FIRST_PROMPT.format(tweet_texts=chr(10).join(texts))

        try:
            resp = llm_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600, temperature=0.3)
            content = (resp.choices[0].message.content or "{}").strip()
            # Strip markdown fences
            if content.startswith("```"):
                # Remove opening ```json or ```
                content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3].strip()
            # Extract JSON object — find first { and matching }
            start = content.find("{")
            if start >= 0:
                content = content[start:]
            result = json.loads(content)
            result["narrative"] = result.get("narrative", "")
            result["themes"] = result.get("themes", [])
            result["sentiment"] = result.get("sentiment", "")
            return result
        except json.JSONDecodeError:
            # Try to salvage: fix truncated JSON by closing unclosed strings
            try:
                logger.debug("Retrying with truncated JSON fix")
                content = content.rstrip()
                if not content.endswith("}"):
                    # Add missing closing
                    content = content + '"}'
                result = json.loads(content)
                return result
            except (json.JSONDecodeError, Exception):
                pass
            logger.warning("Narrative update failed: JSON parse error")
            return None
        except Exception as exc:
            logger.warning("Narrative update failed: %s", exc)
            return None
