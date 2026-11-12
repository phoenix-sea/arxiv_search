import datetime as dt
import json

import httpx

from arxiv_search import db
from arxiv_search.gemini import GeminiAnalyzer
from arxiv_search.pipeline import run_update
from arxiv_search.telegram import Telegram, format_paper
from tests.conftest import make_paper


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def gemini_scoring(scores):
    def handler(req):
        text = json.loads(req.content)["contents"][0]["parts"][0]["text"]
        ids = [l.split('"')[1] for l in text.splitlines() if l.startswith("<paper id=")]
        items = [{"id": i, "relevance": scores[i], "importance": 5, "topics": ["ir"], "tldr": "tl", "reason": "rs"}
                 for i in ids]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(items)}]},
                                                          "finishReason": "STOP"}]})
    return GeminiAnalyzer("k", "gemini-3.5-flash-lite", "p", rpm=0,
                          client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_update_end_to_end(settings, con, no_sleep):
    db.upsert_papers(con, [make_paper("2611.00001", created="2026-11-08")], added_at=None)  # initial index
    batch = [make_paper("2611.00001", created="2026-11-08"), make_paper("2611.00002"), make_paper("2611.00003"),
             make_paper("2611.00004"), make_paper("2611.00005")]
    windows = []

    def fetcher(cats, start, end):
        windows.append((start, end))
        return batch

    scores = {"2611.00002": 9, "2611.00003": 8, "2611.00004": 7, "2611.00005": 2}
    tg = FakeTelegram()
    stats = run_update(settings, con, fetcher=fetcher, analyzer=gemini_scoring(scores), notifier=tg,
                       until=dt.date(2026, 11, 11))
    assert windows[0][0] == dt.date(2026, 11, 5)  # newest stored (11-08) minus LOOKBACK_DAYS
    assert stats == {"fetched": 5, "new": 4, "analyzed": 4, "notified": 2, "affiliated": 0}
    assert "4 new papers" in tg.sent[0] and "3 match" in tg.sent[0] and "1 more in the UI" in tg.sent[0]
    assert "2611.00002" in tg.sent[1] and "2611.00003" in tg.sent[2] and len(tg.sent) == 3

    # second run: nothing new -> no messages, nothing re-sent
    tg2 = FakeTelegram()
    stats2 = run_update(settings, con, fetcher=fetcher, analyzer=gemini_scoring(scores), notifier=tg2)
    assert stats2["new"] == 0 and tg2.sent == []
    run = con.execute("SELECT status FROM runs ORDER BY run_id DESC").fetchone()
    assert run["status"] == "ok"


def test_failure_is_recorded_and_notified(settings, con):
    def fetcher(*a):
        raise RuntimeError("arXiv down")

    tg = FakeTelegram()
    try:
        run_update(settings, con, fetcher=fetcher, analyzer=None, notifier=tg)
    except RuntimeError:
        pass
    assert "update failed" in tg.sent[0]
    assert con.execute("SELECT status FROM runs").fetchone()["status"] == "error"


def test_format_paper_escapes_html():
    row = {"id": "2611.00001", "title": "A <b> & C", "authors": "X; Y", "primary_cat": "cs.IR", "created": "2026",
           "relevance": 8, "importance": 6, "tldr": "uses <script>", "reason": "r", "topics": '["learning to rank"]'}
    out = format_paper(row, "http://pi:8000")
    assert "A &lt;b&gt; &amp; C" in out and "&lt;script&gt;" in out
    assert "#learning_to_rank" in out and "http://pi:8000/paper/2611.00001" in out


def test_telegram_send_payload(no_sleep):
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "result": {}})

    Telegram("TOKEN", "42", client=httpx.Client(transport=httpx.MockTransport(handler)), min_gap=0).send("hi")
    assert seen["url"].endswith("/botTOKEN/sendMessage")
    assert seen["body"]["chat_id"] == "42" and seen["body"]["parse_mode"] == "HTML"
