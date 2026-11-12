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


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def _path(name: str, default: str) -> Path:
    p = Path(_str(name, default)).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass(frozen=True)
class Settings:
    db_path: Path
    categories: tuple[str, ...]
    profile_path: Path
    gemini_api_key: str
    gemini_model: str
    gemini_thinking_level: str
    gemini_batch_size: int
    gemini_rpm: int
    telegram_bot_token: str
    telegram_chat_id: str
    notify_min_relevance: int
    notify_min_importance: int
    notify_max_per_run: int
    public_url: str
    lookback_days: int
    web_host: str
    web_port: int

    @property
    def profile(self) -> str:
        return self.profile_path.read_text(encoding="utf-8").strip()

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    env = env_file or os.environ.get("ARXIV_SEARCH_ENV") or PROJECT_ROOT / ".env"
    load_dotenv(env, override=False)
    cats = tuple(c for c in _str("CATEGORIES", ",".join(DEFAULT_CATEGORIES)).replace(" ", ",").split(",") if c)
    return Settings(
        db_path=_path("DB_PATH", "data/arxiv.db"),
        categories=cats,
        profile_path=_path("PROFILE_PATH", "profile.md"),
        gemini_api_key=_str("GEMINI_API_KEY"),
        gemini_model=_str("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        gemini_thinking_level=_str("GEMINI_THINKING_LEVEL", "minimal"),
        gemini_batch_size=_int("GEMINI_BATCH_SIZE", 20),
        gemini_rpm=_int("GEMINI_RPM", 10),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        notify_min_relevance=_int("NOTIFY_MIN_RELEVANCE", 7),
        notify_min_importance=_int("NOTIFY_MIN_IMPORTANCE", 0),
        notify_max_per_run=_int("NOTIFY_MAX_PER_RUN", 15),
        public_url=_str("PUBLIC_URL", "http://raspberrypi.local:8000").rstrip("/"),
        lookback_days=_int("LOOKBACK_DAYS", 3),
        web_host=_str("WEB_HOST", "0.0.0.0"),
        web_port=_int("WEB_PORT", 8000),
    )
