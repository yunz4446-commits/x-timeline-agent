"""深度总结引擎 — 阅读全量推文原文，发现热点话题，归纳观点，交叉引用。

独立于 narrative 系统和 query_timeline。仅响应用户明确的总结请求。
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from ..db.repository import get_all_tweets_since

logger = logging.getLogger(__name__)

STATE_FILE = "data/summary.json"

SUMMARIZE_PROMPT = """你是加密货币+AI市场观察者。请阅读以下X推文，写一份深度总结。

## 上一轮总结（作为上下文参考）
{previous_summary}

## 新推文（按时间升序排列，共 {tweet_count} 条）
{tweet_texts}

## 核心要求
你的任务是像人类刷X一样，发现哪些话题/币种/项目/事件/人物被反复提及，并把不同人的观点串起来。

### 发现热点话题
- 扫描全部推文，统计哪些币种、项目、事件、人名反复出现
- 一个话题不需要所有人都在聊，只要有2条以上涉及同一事物就算热点
- 不要漏掉小众但讨论质量高的话题

### 归纳观点细节
对每个热点话题，具体写出：
- 谁说了什么（@作者 具体观点，引用原文关键词）
- 不同人之间是否有共识或分歧
- 如果有KOL表态，明确指出是看多还是看空

### 交叉对比
- 标注观点之间的关系："A和B都看好X" / "C和D对X持相反看法，C认为...D认为..."
- 如果某话题在上轮总结中已出现，说明是"延续"还是"变化"
- 如果两个独立话题之间存在因果或关联，指出来

### 整体情绪
偏多/偏空/分歧/观望

### 返回格式
返回纯 JSON（不要 markdown）：
{{
  "overview": "总览：过去一段时间整体在聊什么（2-3句中文，具体到币种/事件/人名）",
  "topics": [
    {{
      "topic": "话题名（具体到币种/项目/事件/人名，不要用抽象类别）",
      "mention_count": 提及次数,
      "is_continuation": true或false,
      "sentiment": "偏多/偏空/分歧/观望",
      "perspectives": [
        {{"author": "作者用户名（不带@）", "angle": "具体观点（引用原文关键信息）", "tweet_id": "推文ID", "time": "HH:MM"}}
      ],
      "cross_ref": "这个topic内不同观点的关系描述，如：xxx和xxx都看好，但xxx认为风险在于..."
    }}
  ],
  "sentiment": "偏多/偏空/分歧/观望",
  "tweet_count": 总推文数,
  "covered_count": 被话题覆盖的推文数
}}

注意：
- 不要用"某个币""某项目""某KOL"等模糊表述，必须写出具体名字
- perspectives 中每一条必须来自一条具体的推文，tweet_id 必须准确
- 如果推文内容涉及投资建议，仅客观转述，不判断对错"""

# 独立总结 prompt（无上轮上下文，用于定时推送等固定窗口场景）
STANDALONE_PROMPT = """你是加密货币+AI市场观察者。请阅读以下X推文，写一份深度总结。

## 新推文（按时间升序排列，共 {tweet_count} 条）
{tweet_texts}

## 核心要求
你的任务是像人类刷X一样，发现哪些话题/币种/项目/事件/人物被反复提及，并把不同人的观点串起来。

### 发现热点话题
- 扫描全部推文，统计哪些币种、项目、事件、人名反复出现
- 一个话题不需要所有人都在聊，只要有2条以上涉及同一事物就算热点
- 不要漏掉小众但讨论质量高的话题

### 归纳观点细节
对每个热点话题，具体写出：
- 谁说了什么（@作者 具体观点，引用原文关键词）
- 不同人之间是否有共识或分歧
- 如果有KOL表态，明确指出是看多还是看空

### 交叉对比
- 标注观点之间的关系："A和B都看好X" / "C和D对X持相反看法，C认为...D认为..."
- 如果两个独立话题之间存在因果或关联，指出来

### 整体情绪
偏多/偏空/分歧/观望

### 返回格式
返回纯 JSON（不要 markdown）：
{{
  "overview": "总览：这段时间整体在聊什么（2-3句中文，具体到币种/事件/人名）",
  "topics": [
    {{
      "topic": "话题名（具体到币种/项目/事件/人名）",
      "mention_count": 提及次数,
      "sentiment": "偏多/偏空/分歧/观望",
      "perspectives": [
        {{"author": "作者用户名（不带@）", "angle": "具体观点（引用原文关键信息）", "tweet_id": "推文ID", "time": "HH:MM"}}
      ],
      "cross_ref": "这个topic内不同观点的关系描述"
    }}
  ],
  "sentiment": "偏多/偏空/分歧/观望",
  "tweet_count": 总推文数,
  "covered_count": 被话题覆盖的推文数
}}

注意：
- 不要用"某个币""某项目""某KOL"等模糊表述，必须写出具体名字
- perspectives 中每一条必须来自一条具体的推文，tweet_id 必须准确
- 如果推文内容涉及投资建议，仅客观转述，不判断对错"""


def _repair_truncated_json(text: str) -> str | None:
    """尝试修复被 token 限制截断的 JSON。

    截断通常发生在某个 topic 的 perspectives 数组中间或字符串中间。
    策略：去掉末尾被截断的不完整片段，按嵌套顺序补全缺失的闭合括号。
    """
    # 1. 如果末尾在字符串内，去掉被截断的字符串片段
    in_str = False
    for ch in text:
        if ch == '"':
            in_str = not in_str
    if in_str:
        last_quote = text.rfind('"')
        if last_quote > 0:
            text = text[:last_quote + 1] + '"'

    # 2. 去掉末尾逗号
    text = text.rstrip().rstrip(',').rstrip()

    # 3. 按 LIFO 顺序补全闭合括号
    stack = []
    in_string = False
    for ch in text:
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            stack.append('}')
        elif ch == '[':
            stack.append(']')
        elif ch == '}':
            if stack and stack[-1] == '}':
                stack.pop()
        elif ch == ']':
            if stack and stack[-1] == ']':
                stack.pop()

    text += ''.join(reversed(stack))

    if text.count('{') == text.count('}') and text.count('[') == text.count(']'):
        return text
    return None


class SummarizeManager:
    """管理深度总结的状态和生成。"""

    @staticmethod
    def _load() -> dict | None:
        try:
            return json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @staticmethod
    def _save(state: dict) -> None:
        Path(STATE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(STATE_FILE).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def summarize(self, llm, model: str, session: Session,
                  since: datetime | None = None) -> dict:
        """执行深度总结。

        llm 是 OpenAI client，model 是模型名。
        since 为 None 时：从上次总结到现在（用于 chat 交互式总结）。
        since 指定时：固定时间窗口（用于定时 digest 推送），不加载/不持久化状态。
        """
        standalone = since is not None

        if standalone:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            old_state = None
        else:
            old_state = self._load()
            if old_state and old_state.get("last_summary_at"):
                since = datetime.fromisoformat(old_state["last_summary_at"])
                if since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
            else:
                since = datetime.now(timezone.utc) - timedelta(hours=2)

        # 全量获取推文
        tweets = get_all_tweets_since(session, since)
        if not tweets:
            if not standalone and old_state and old_state.get("last_result"):
                cached = old_state["last_result"]
                cached["cached"] = True
                cached["since"] = str(since)
                return cached
            return {
                "status": "empty",
                "message": "这段时间没有新推文。",
                "since": str(since),
            }

        # 去噪：跳过空文本
        valid_tweets = [t for t in tweets if t.text and t.text.strip()]
        if not valid_tweets:
            return {
                "status": "empty",
                "message": "这段时间的推文均为空内容。",
                "since": str(since),
            }

        # 格式化推文
        lines = []
        for i, t in enumerate(valid_tweets, 1):
            ts = ""
            if t.tweet_created_at:
                ts = t.tweet_created_at.strftime("%H:%M")
            text = t.text.strip()[:500]
            lines.append(f"[{i}] @{t.author_username} {ts} tweet_id={t.tweet_id}\n    {text}")

        if standalone:
            prompt = STANDALONE_PROMPT.format(
                tweet_count=len(valid_tweets),
                tweet_texts="\n".join(lines),
            )
        else:
            prev_text = "这是第一次总结，没有上一轮参考。"
            if old_state and old_state.get("last_result"):
                lr = old_state["last_result"]
                prev_text = json.dumps({
                    "overview": lr.get("overview", ""),
                    "topics": [{"topic": tp.get("topic", ""),
                                "sentiment": tp.get("sentiment", "")}
                               for tp in lr.get("topics", [])],
                    "sentiment": lr.get("sentiment", ""),
                }, ensure_ascii=False)

            prompt = SUMMARIZE_PROMPT.format(
                previous_summary=prev_text,
                tweet_count=len(valid_tweets),
                tweet_texts="\n".join(lines),
            )

        # 调 LLM
        resp = llm.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8000,
            temperature=0.3,
        )
        content = (resp.choices[0].message.content or "{}").strip()

        # 检查是否因 token 不足被截断
        if resp.choices[0].finish_reason == "length":
            logger.warning("Summarize response truncated (max_tokens exceeded)")

        # 解析 JSON
        result = self._parse_json(content)
        result["tweet_count"] = len(valid_tweets)
        result["covered_count"] = result.get("covered_count", 0)
        result["since"] = str(since)

        # 持久化（仅交互模式）
        if not standalone:
            latest_ts = max(
                (t.tweet_created_at for t in valid_tweets if t.tweet_created_at),
                default=datetime.now(timezone.utc),
            )
            if latest_ts.tzinfo is None:
                latest_ts = latest_ts.replace(tzinfo=timezone.utc)

            self._save({
                "last_summary_at": datetime.now(timezone.utc).isoformat(),
                "last_tweet_created_at": latest_ts.isoformat(),
                "last_result": result,
            })

        return result

    @staticmethod
    def _parse_json(content: str) -> dict:
        """从 LLM 响应中提取 JSON，带有容错处理。"""
        # 去除 markdown fences
        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            if len(lines) > 1:
                cleaned = "\n".join(lines[1:])
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

        # 找到 JSON 对象的起止位置
        start = cleaned.find("{")
        if start >= 0:
            # 找到匹配的 }
            depth = 0
            end = -1
            for i in range(start, len(cleaned)):
                if cleaned[i] == "{":
                    depth += 1
                elif cleaned[i] == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                cleaned = cleaned[start:end]

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            # 尝试修复截断：从最后一个完整结构处截断，补全闭合
            repaired = _repair_truncated_json(cleaned)
            if repaired:
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass

            logger.warning("Summarize JSON parse failed at pos %s: %s...",
                           exc.pos, content[exc.pos:exc.pos + 80] if exc.pos else "")
            return {
                "overview": "（解析总结结果时出错，请重试）",
                "topics": [],
                "sentiment": "未知",
                "parse_error": True,
            }
