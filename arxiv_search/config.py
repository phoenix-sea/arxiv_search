"""Settings, read from environment variables and the project's .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATEGORIES = ("cs.IR", "cs.CL", "cs.AI", "cs.LG")


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    db_path: Path
    categories: tuple[str, ...]
    gemini_api_key: str
    gemini_model: str
    telegram_bot_token: str
    telegram_chat_id: str
    web_port: int


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    db = Path(_str("DB_PATH", "data/arxiv.db")).expanduser()
    return Settings(
        db_path=db if db.is_absolute() else PROJECT_ROOT / db,
        categories=tuple(c for c in _str("CATEGORIES", ",".join(DEFAULT_CATEGORIES)).replace(" ", ",").split(",") if c),
        gemini_api_key=_str("GEMINI_API_KEY"),
        gemini_model=_str("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        web_port=int(_str("WEB_PORT", "8000")),
    )
