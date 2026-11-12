"""SQLite storage.

papers + papers_fts: the index (FTS kept in sync by triggers).
paper_orgs + orgs_fts: affiliations parsed from each paper's author block
  (orgs_fts rowid = papers.rowid); affil_status: which papers were processed.
flags: Gemini's interesting yes/no for new papers; notifications: sent to Telegram.
labels: the user's judgments from the UI.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Sequence

from .models import Org, Paper

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  id TEXT PRIMARY KEY, title TEXT, abstract TEXT, authors TEXT, n_authors INT,
  categories TEXT, primary_cat TEXT, created TEXT, updated TEXT, license TEXT,
  doi TEXT, journal_ref TEXT, comments TEXT, datestamp TEXT);
CREATE INDEX IF NOT EXISTS idx_papers_created ON papers(created);
CREATE TABLE IF NOT EXISTS paper_orgs(
  paper_id TEXT, pos INT, name TEXT, unit TEXT, domain TEXT, PRIMARY KEY(paper_id, pos));
CREATE INDEX IF NOT EXISTS idx_paper_orgs_name ON paper_orgs(name);
CREATE TABLE IF NOT EXISTS affil_status(paper_id TEXT PRIMARY KEY, status TEXT, source TEXT, extracted_at TEXT);
CREATE TABLE IF NOT EXISTS flags(paper_id TEXT PRIMARY KEY, interesting INT, model TEXT, flagged_at TEXT);
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, sent_at TEXT);
CREATE TABLE IF NOT EXISTS labels(paper_id TEXT PRIMARY KEY, label TEXT, labeled_at TEXT);
"""

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(title, abstract, authors,
  content='papers', content_rowid='rowid', tokenize='porter unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS orgs_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
"""

TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
  INSERT INTO papers_fts(rowid, title, abstract, authors) VALUES (new.rowid, new.title, new.abstract, new.authors);
END;
CREATE TRIGGER IF NOT EXISTS papers_ad AFTER DELETE ON papers BEGIN
  INSERT INTO papers_fts(papers_fts, rowid, title, abstract, authors) VALUES ('delete', old.rowid, old.title, old.abstract, old.authors);
END;
CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE OF title, abstract, authors ON papers BEGIN
  INSERT INTO papers_fts(papers_fts, rowid, title, abstract, authors) VALUES ('delete', old.rowid, old.title, old.abstract, old.authors);
  INSERT INTO papers_fts(rowid, title, abstract, authors) VALUES (new.rowid, new.title, new.abstract, new.authors);
END;
"""

UPSERT = """
INSERT INTO papers(id, title, abstract, authors, n_authors, categories, primary_cat, created,
                   updated, license, doi, journal_ref, comments, datestamp, added_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET
  title=excluded.title, abstract=excluded.abstract, authors=excluded.authors,
  n_authors=excluded.n_authors, categories=excluded.categories, primary_cat=excluded.primary_cat,
  created=COALESCE(papers.created, excluded.created),
  updated=COALESCE(excluded.updated, papers.updated),
  license=COALESCE(excluded.license, papers.license),
  doi=COALESCE(excluded.doi, papers.doi),
  journal_ref=COALESCE(excluded.journal_ref, papers.journal_ref),
  comments=COALESCE(excluded.comments, papers.comments),
  datestamp=COALESCE(excluded.datestamp, papers.datestamp)
WHERE papers.title IS NOT excluded.title OR papers.abstract IS NOT excluded.abstract
   OR papers.authors IS NOT excluded.authors OR papers.categories IS NOT excluded.categories
   OR (excluded.updated IS NOT NULL AND papers.updated IS NOT excluded.updated)
"""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=60, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=60000")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(papers)")}
    if "added_at" not in cols:  # migration for DBs built by ~/arxiv-index
        con.execute("ALTER TABLE papers ADD COLUMN added_at TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_papers_added ON papers(added_at)")
    had_fts = con.execute("SELECT 1 FROM sqlite_master WHERE name='papers_fts'").fetchone()
    con.executescript(FTS)
    if not had_fts and con.execute("SELECT 1 FROM papers LIMIT 1").fetchone():
        con.execute("INSERT INTO papers_fts(papers_fts) VALUES('rebuild')")
    con.executescript(TRIGGERS)
    con.commit()


def paper_from_row(r: sqlite3.Row) -> Paper:
    return Paper(
        id=r["id"], title=r["title"] or "", abstract=r["abstract"] or "",
        authors=r["authors"].split("; ") if r["authors"] else [],
        categories=(r["categories"] or "").split(), created=r["created"], updated=r["updated"],
        license=r["license"], doi=r["doi"], journal_ref=r["journal_ref"], comments=r["comments"],
        datestamp=r["datestamp"])


def existing_ids(con: sqlite3.Connection, ids: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f"SELECT id FROM papers WHERE id IN ({','.join('?' * len(chunk))})"
        out.update(r[0] for r in con.execute(q, chunk))
    return out


def upsert_papers(con: sqlite3.Connection, papers: Sequence[Paper], added_at: str | None) -> list[str]:
    """Insert/refresh papers. Returns ids that were not in the DB before.
    `added_at` marks newly discovered papers (None for bulk imports)."""
    if not papers:
        return []
    have = existing_ids(con, [p.id for p in papers])
    new: list[str] = []
    rows = []
    for p in papers:
        is_new = p.id not in have
        if is_new:
            new.append(p.id)
            have.add(p.id)  # guard against duplicates within one batch
        rows.append((p.id, p.title, p.abstract, "; ".join(p.authors), len(p.authors), " ".join(p.categories),
                     p.primary_cat, p.created, p.updated, p.license, p.doi, p.journal_ref, p.comments,
                     p.datestamp, added_at if is_new else None))
    con.executemany(UPSERT, rows)
    con.commit()
    return new


def max_created(con: sqlite3.Connection) -> str | None:
    return con.execute("SELECT max(created) FROM papers").fetchone()[0]


def _in(ids: Sequence[str]) -> str:
    return ",".join("?" * len(ids))


# -- affiliations ------------------------------------------------------------

def pending_affiliations(con: sqlite3.Connection, *, only_added: bool, limit: int | None = None) -> list[Paper]:
    """Papers whose affiliations were not parsed yet, newest first."""
    sql = ("SELECT p.* FROM papers p LEFT JOIN affil_status s ON s.paper_id = p.id WHERE s.paper_id IS NULL"
           + (" AND p.added_at IS NOT NULL" if only_added else "") + " ORDER BY p.created DESC, p.id DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [paper_from_row(r) for r in con.execute(sql)]


def save_affiliations(con: sqlite3.Connection, pid: str, orgs: Sequence[Org], *, source: str | None,
                      status: str = "ok") -> None:
    row = con.execute("SELECT rowid FROM papers WHERE id = ?", (pid,)).fetchone()
    if not row:
        return
    con.execute("DELETE FROM paper_orgs WHERE paper_id = ?", (pid,))
    con.execute("DELETE FROM orgs_fts WHERE rowid = ?", (row[0],))
    con.executemany("INSERT INTO paper_orgs(paper_id, pos, name, unit, domain) VALUES (?,?,?,?,?)",
                    [(pid, i, o.name, o.unit, o.domain) for i, o in enumerate(orgs)])
    if orgs:
        text = " ; ".join(" ".join(x for x in (o.name, o.unit, o.domain) if x) for o in orgs)
        con.execute("INSERT INTO orgs_fts(rowid, text) VALUES (?, ?)", (row[0], text))
    con.execute("INSERT OR REPLACE INTO affil_status(paper_id, status, source, extracted_at) VALUES (?,?,?,?)",
                (pid, status, source, now_iso()))
    con.commit()


def orgs_for(con: sqlite3.Connection, ids: Sequence[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for i in range(0, len(ids), 500):
        chunk = list(ids[i:i + 500])
        for r in con.execute(f"SELECT paper_id, name FROM paper_orgs WHERE paper_id IN ({_in(chunk)}) "
                             f"ORDER BY paper_id, pos", chunk):
            out.setdefault(r[0], []).append(r[1])
    return out


# -- Gemini flags and Telegram -------------------------------------------------

def unflagged_new(con: sqlite3.Connection) -> list[Paper]:
    """Papers found by the crawler that Gemini has not judged yet."""
    return [paper_from_row(r) for r in con.execute(
        "SELECT p.* FROM papers p LEFT JOIN flags f ON f.paper_id = p.id "
        "WHERE p.added_at IS NOT NULL AND f.paper_id IS NULL ORDER BY p.created DESC")]


def save_flags(con: sqlite3.Connection, flags: dict[str, bool], model: str) -> None:
    ts = now_iso()
    con.executemany("INSERT OR REPLACE INTO flags VALUES (?,?,?,?)",
                    [(pid, int(v), model, ts) for pid, v in flags.items()])
    con.commit()


def to_notify(con: sqlite3.Connection) -> list[Paper]:
    return [paper_from_row(r) for r in con.execute(
        "SELECT p.* FROM papers p JOIN flags f ON f.paper_id = p.id LEFT JOIN notifications n ON n.id = p.id "
        "WHERE f.interesting = 1 AND n.id IS NULL ORDER BY p.created DESC")]


def mark_notified(con: sqlite3.Connection, pid: str) -> None:
    con.execute("INSERT OR IGNORE INTO notifications VALUES (?, ?)", (pid, now_iso()))
    con.commit()


# -- labels ----------------------------------------------------------------------

def set_label(con: sqlite3.Connection, pid: str, label: str | None) -> None:
    if label:
        con.execute("INSERT OR REPLACE INTO labels VALUES (?,?,?)", (pid, label, now_iso()))
    else:
        con.execute("DELETE FROM labels WHERE paper_id = ?", (pid,))
    con.commit()


def labels_for(con: sqlite3.Connection, ids: Sequence[str]) -> dict[str, str]:
    ids = list(ids)
    return dict(con.execute(f"SELECT paper_id, label FROM labels WHERE paper_id IN ({_in(ids)})", ids).fetchall()) \
        if ids else {}
