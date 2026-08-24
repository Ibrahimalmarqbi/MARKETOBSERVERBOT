from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    database_url: str
    admin_api_key: str
    default_interval: str
    alert_poll_seconds: int
    paper_trading: bool
    max_risk_percent: float
    max_position_notional: float
    allow_live_trading: bool
    port: int
    llm_api_key: str
    llm_api_base: str
    llm_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("TELEGRAM_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_TOKEN is required")

        database_url = os.getenv("DATABASE_URL", "sqlite:///marketobserver_v2.db").strip()
        if database_url.startswith("postgres://"):
            database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
        elif database_url.startswith("postgresql://") and "+psycopg" not in database_url:
            database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        admin_key = os.getenv("ADMIN_API_KEY", "").strip()
        if not admin_key:
            raise RuntimeError("ADMIN_API_KEY is required")

        return cls(
            telegram_token=token,
            database_url=database_url,
            admin_api_key=admin_key,
            default_interval=os.getenv("DEFAULT_INTERVAL", "1h"),
            alert_poll_seconds=max(15, int(os.getenv("ALERT_POLL_SECONDS", "60"))),
            paper_trading=os.getenv("PAPER_TRADING", "true").lower() in {"1", "true", "yes", "on"},
            max_risk_percent=float(os.getenv("MAX_RISK_PERCENT", "2")),
            max_position_notional=float(os.getenv("MAX_POSITION_NOTIONAL", "10000")),
            allow_live_trading=os.getenv("ALLOW_LIVE_TRADING", "false").lower() in {"1", "true", "yes", "on"},
            port=int(os.getenv("PORT", "10000")),
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            llm_api_base=os.getenv("LLM_API_BASE", "https://api.openai.com/v1").strip(),
            llm_model=os.getenv("LLM_MODEL", "gpt-5-mini").strip(),
        )