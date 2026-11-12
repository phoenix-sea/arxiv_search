import dataclasses

import pytest

from arxiv_search import db
from arxiv_search.config import Settings
from arxiv_search.models import Paper


@pytest.fixture
def settings(tmp_path):
    prof = tmp_path / "profile.md"
    prof.write_text("I care about ranking and retrieval.")
    return Settings(
        db_path=tmp_path / "t.db", categories=("cs.IR", "cs.CL", "cs.AI", "cs.LG"), profile_path=prof,
        gemini_api_key="k", gemini_model="gemini-3.5-flash-lite", gemini_thinking_level="minimal",
        gemini_batch_size=2, gemini_rpm=0, telegram_bot_token="t", telegram_chat_id="1",
        notify_min_relevance=7, notify_min_importance=0, notify_max_per_run=2,
        public_url="http://pi:8000", lookback_days=3, web_host="127.0.0.1", web_port=8000)


@pytest.fixture
def con(settings):
    c = db.connect(settings.db_path)
    db.init_db(c)
    yield c
    c.close()


def make_paper(pid="2611.00001", **kw):
    base = dict(id=pid, title=f"Learning to rank paper {pid}", abstract="We study neural ranking with LLM rerankers.",
                authors=["Ada Lovelace", "Alan Turing"], categories=["cs.IR", "cs.CL"], created="2026-11-09")
    base.update(kw)
    return Paper(**base)


@pytest.fixture
def paper_factory():
    return make_paper


@pytest.fixture
def no_sleep(monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
