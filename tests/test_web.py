from fastapi.testclient import TestClient

from arxiv_search import db
from arxiv_search.models import Assessment
from arxiv_search.web import create_app
from tests.conftest import make_paper


def client(settings, con):
    db.upsert_papers(con, [make_paper("2611.00001", title="LLM rerankers for <semantic> search"),
                           make_paper("2611.00002", title="Graph transformers", abstract="Molecules.",
                                      categories=["cs.LG"]),
                           make_paper("cs/0101001", title="Old IR classic", created="2001-01-01")],
                      added_at=db.now_iso())
    db.save_assessments(con, [Assessment("2611.00001", 9, 7, ["reranking"], "Rerankers work.", "core topic")],
                        "gemini-3.5-flash-lite")
    db.record_usage(con, "gemini-3.5-flash-lite", 1, 1000, 200)
    return TestClient(create_app(settings))


def test_search_and_filters(settings, con):
    c = client(settings, con)
    r = c.get("/", params={"q": "rerankers"})
    assert r.status_code == 200
    assert "LLM rerankers for &lt;semantic&gt; search" in r.text and "Graph transformers" not in r.text
    assert "<mark>" in r.text or "Rerankers work." in r.text
    r = c.get("/", params={"q": "", "cat": "cs.LG"})
    assert "Graph transformers" in r.text and "LLM rerankers" not in r.text
    r = c.get("/", params={"min_rel": "7"})
    assert "LLM rerankers" in r.text and "Graph transformers" not in r.text


def test_landing_shows_new_papers(settings, con):
    r = client(settings, con).get("/")
    assert r.status_code == 200 and "3 papers found by the updater" in r.text


def test_landing_default_not_baked_into_form(settings, con):
    # the 7-day landing view must not pre-select filters that the next search would inherit
    r = client(settings, con).get("/")
    assert '<option value="7" selected>' not in r.text
    assert '<option value="relevance" selected>' not in r.text
    assert '<option value="best" selected>' in r.text

def test_bad_fts_syntax_falls_back(settings, con):
    r = client(settings, con).get("/", params={"q": 'rerankers AND ("'})
    assert r.status_code == 200 and "LLM rerankers" in r.text


def test_paper_pages(settings, con):
    c = client(settings, con)
    r = c.get("/paper/2611.00001")
    assert r.status_code == 200 and "Relevance 9/10" in r.text
    assert c.get("/paper/cs/0101001").status_code == 200
    assert c.get("/paper/9999.99999").status_code == 404


def test_stats_and_health(settings, con):
    c = client(settings, con)
    r = c.get("/stats")
    assert r.status_code == 200 and "$0.00" in r.text
    assert c.get("/healthz").text == "ok"
