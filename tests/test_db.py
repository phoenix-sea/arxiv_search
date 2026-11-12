import sqlite3

from arxiv_search import db


def fts_ids(con, q):
    return [r[0] for r in con.execute(
        "SELECT p.id FROM papers_fts f JOIN papers p ON p.rowid=f.rowid WHERE papers_fts MATCH ?", (q,))]


def test_upsert_reports_only_new(con, paper_factory):
    a, b = paper_factory("2611.00001"), paper_factory("2611.00002")
    assert db.upsert_papers(con, [a], added_at="2026-11-10T00:00:00+00:00") == ["2611.00001"]
    assert db.upsert_papers(con, [a, b, b], added_at="2026-11-11T00:00:00+00:00") == ["2611.00002"]
    rows = dict(con.execute("SELECT id, added_at FROM papers").fetchall())
    assert rows["2611.00001"].startswith("2026-11-10")  # not overwritten by re-fetch


def test_fts_follows_inserts_and_updates(con, paper_factory):
    db.upsert_papers(con, [paper_factory("2611.00001", title="Zebra retrieval")], added_at=None)
    assert fts_ids(con, "zebra") == ["2611.00001"]
    db.upsert_papers(con, [paper_factory("2611.00001", title="Giraffe retrieval")], added_at=None)
    assert fts_ids(con, "zebra") == []
    assert fts_ids(con, "giraffe") == ["2611.00001"]
    assert fts_ids(con, "ranked") == ["2611.00001"]  # porter stemming: ranking ~ ranked


def test_migrates_arxiv_index_db(tmp_path):
    """DBs built by ~/arxiv-index have papers + papers_fts but no added_at/triggers."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
      CREATE TABLE papers(id TEXT PRIMARY KEY, title TEXT, abstract TEXT, authors TEXT, n_authors INT,
        categories TEXT, primary_cat TEXT, created TEXT, updated TEXT, license TEXT, doi TEXT,
        journal_ref TEXT, comments TEXT, datestamp TEXT);
      INSERT INTO papers(id, title, abstract, authors, categories, created)
        VALUES ('2301.00001', 'Old dense retrieval', 'abs', 'A B', 'cs.IR', '2023-01-01');
      CREATE VIRTUAL TABLE papers_fts USING fts5(title, abstract, authors, content='papers',
        content_rowid='rowid', tokenize='porter unicode61');
      INSERT INTO papers_fts(papers_fts) VALUES('rebuild');""")
    old.commit(); old.close()
    con = db.connect(path)
    db.init_db(con)
    assert fts_ids(con, "dense") == ["2301.00001"]
    from tests.conftest import make_paper
    db.upsert_papers(con, [make_paper("2611.00009", title="Fresh sparse retrieval")], added_at="x")
    assert fts_ids(con, "sparse") == ["2611.00009"]
    assert db.pending_analysis(con, only_added=True)[0].id == "2611.00009"


def test_notification_candidates_and_marking(con, paper_factory):
    from arxiv_search.models import Assessment
    db.upsert_papers(con, [paper_factory("2611.00001"), paper_factory("2611.00002")], added_at="now")
    db.upsert_papers(con, [paper_factory("2301.00001")], added_at=None)  # bulk-imported: never notified
    db.save_assessments(con, [Assessment("2611.00001", 9, 5), Assessment("2611.00002", 4, 9),
                              Assessment("2301.00001", 10, 10)], "m")
    assert [r["id"] for r in db.notification_candidates(con, 7, 0)] == ["2611.00001"]
    db.mark_notified(con, ["2611.00001"])
    assert db.notification_candidates(con, 7, 0) == []
