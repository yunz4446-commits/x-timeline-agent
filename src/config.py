"""配置加载模块 — 从 config.yaml 和环境变量加载配置"""

import os
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

load_dotenv()


def _env_or(key: str, fallback: str = "") -> str:
    return os.getenv(key, fallback)


class Config:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # LLM
        self.llm_provider: str = raw["llm"]["provider"]
        self.llm_model: str = raw["llm"]["model"]
        self.llm_api_key: str = _env_or("LLM_API_KEY", raw["llm"].get("api_key", ""))
        self.llm_api_base: str = raw["llm"].get("api_base", "https://api.deepseek.com")

        # X — login via browser, no API key needed
        self.x_bearer_token: str = ""  # unused, kept for compat
        self.x_api_base_url: str = "https://x.com"

        # Feishu
        fc = raw.get("channels", {}).get("feishu", {})
        self.feishu_enabled: bool = fc.get("enabled", True)
        self.feishu_webhook_url: str = _env_or("FEISHU_WEBHOOK_URL", fc.get("webhook_url", ""))
        self.feishu_app_id: str = _env_or("FEISHU_APP_ID", fc.get("app_id", ""))
        self.feishu_app_secret: str = _env_or("FEISHU_APP_SECRET", fc.get("app_secret", ""))

        # Schedule
        sc = raw["schedule"]
        self.digest_times: list = sc["digest_times"]
        self.fetch_interval_minutes: int = sc["fetch_interval_minutes"]
        self.following_sync_interval_minutes: int = sc["following_sync_interval_minutes"]
        self.timezone: str = sc["timezone"]

        # Classifier
        cl = raw["classifier"]
        self.min_interest_score: float = cl["min_score"]
        self.max_items_per_digest: int = cl["max_items_per_digest"]

        # Storage
        self.db_path: str = raw["storage"]["db_path"]

    @property
    def valid(self) -> bool:
        issues = []
        # X login via browser, not API — no bearer token needed
        if not self.llm_api_key:
            issues.append("缺少 LLM API Key")
        if self.feishu_enabled and not self.feishu_webhook_url:
            issues.append("飞书已启用但缺少 webhook_url")
        return len(issues) == 0, issues


_config: Config | None = None


def load_config(config_path: str = "config.yaml") -> Config:
    global _config
    _config = Config(config_path)
    return _config


def get_config() -> Config:
    if _config is None:
        return load_config()
    return _config
