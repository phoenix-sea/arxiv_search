"""SQLite storage: papers + FTS5 index (kept in sync by triggers), Gemini
assessments, notification log, update-run log and token usage.

Affiliations: paper_orgs holds one row per organization of a paper, orgs_fts
indexes them for search (rowid = papers.rowid), affil_status records which
papers were processed.

Compatible with the arxiv_meta.db built by ~/arxiv-index (same papers table);
init_db() migrates it in place.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

from .models import Assessment, Org, Paper

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers(
  id TEXT PRIMARY KEY, title TEXT, abstract TEXT, authors TEXT, n_authors INT,
  categories TEXT, primary_cat TEXT, created TEXT, updated TEXT, license TEXT,
  doi TEXT, journal_ref TEXT, comments TEXT, datestamp TEXT);
CREATE TABLE IF NOT EXISTS analysis(
  id TEXT PRIMARY KEY, relevance INT, importance INT, topics TEXT, tldr TEXT,
  reason TEXT, model TEXT, analyzed_at TEXT);
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, sent_at TEXT);
CREATE TABLE IF NOT EXISTS runs(
  run_id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, window_from TEXT,
  window_to TEXT, fetched INT, new INT, analyzed INT, notified INT, status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS usage(
  day TEXT, model TEXT, calls INT, input_tokens INT, output_tokens INT,
  PRIMARY KEY(day, model));
CREATE INDEX IF NOT EXISTS idx_papers_created ON papers(created);
CREATE INDEX IF NOT EXISTS idx_analysis_relevance ON analysis(relevance);
CREATE TABLE IF NOT EXISTS paper_orgs(
  paper_id TEXT, pos INT, name TEXT, unit TEXT, domain TEXT, authors TEXT, PRIMARY KEY(paper_id, pos));
CREATE TABLE IF NOT EXISTS affil_status(paper_id TEXT PRIMARY KEY, status TEXT, source TEXT, extracted_at TEXT);
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


def name_tokens(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "")
    return re.findall(r"\w+", "".join(c for c in s if not unicodedata.combining(c)).lower())


def _tok_eq(a: str, b: str) -> bool:
    return a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))


def author_match(authors: str | None, query: str | None) -> bool:
    """True if one author's name contains the query's name parts in order. Middle names may be
    skipped and initials match full names: "Samuel Fraiberger" matches "Samuel P. Fraiberger"
    and "S Fraiberger"; a surname alone matches any author with it."""
    q = name_tokens(query or "")
    if not q:
        return False
    for name in (authors or "").split(";"):
        it = iter(name_tokens(name))
        if all(any(_tok_eq(t, n) for n in it) for t in q):
            return True
    return False


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=60, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.create_function("author_match", 2, author_match, deterministic=True)
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


def pending_analysis(con: sqlite3.Connection, *, only_added: bool = False, since: str | None = None,
                     limit: int | None = None) -> list[Paper]:
    where = ["a.id IS NULL"]
    params: list = []
    if only_added:
        where.append("p.added_at IS NOT NULL")
    if since:
        where.append("p.created >= ?")
        params.append(since)
    sql = (f"SELECT p.* FROM papers p LEFT JOIN analysis a ON a.id = p.id WHERE {' AND '.join(where)} "
           f"ORDER BY p.created DESC, p.id DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [paper_from_row(r) for r in con.execute(sql, params)]


def count_pending(con: sqlite3.Connection, *, since: str | None = None) -> int:
    sql = "SELECT count(*) FROM papers p LEFT JOIN analysis a ON a.id = p.id WHERE a.id IS NULL"
    return con.execute(sql + (" AND p.created >= ?" if since else ""), (since,) if since else ()).fetchone()[0]


def save_assessments(con: sqlite3.Connection, items: Iterable[Assessment], model: str) -> None:
    ts = now_iso()
    con.executemany(
        "INSERT OR REPLACE INTO analysis VALUES (?,?,?,?,?,?,?,?)",
        [(a.id, a.relevance, a.importance, json.dumps(a.topics), a.tldr, a.reason, model, ts) for a in items])
    con.commit()


def record_usage(con: sqlite3.Connection, model: str, calls: int, input_tokens: int, output_tokens: int) -> None:
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    con.execute(
        "INSERT INTO usage VALUES (?,?,?,?,?) ON CONFLICT(day, model) DO UPDATE SET "
        "calls=calls+excluded.calls, input_tokens=input_tokens+excluded.input_tokens, "
        "output_tokens=output_tokens+excluded.output_tokens",
        (day, model, calls, input_tokens, output_tokens))
    con.commit()


def notification_candidates(con: sqlite3.Connection, min_relevance: int, min_importance: int) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT p.*, a.relevance, a.importance, a.topics, a.tldr, a.reason
           FROM papers p JOIN analysis a ON a.id = p.id
           LEFT JOIN notifications n ON n.id = p.id
           WHERE p.added_at IS NOT NULL AND n.id IS NULL AND a.relevance >= ? AND a.importance >= ?
           ORDER BY a.relevance DESC, a.importance DESC, p.created DESC""",
        (min_relevance, min_importance)).fetchall()


def mark_notified(con: sqlite3.Connection, ids: Iterable[str]) -> None:
    ts = now_iso()
    con.executemany("INSERT OR IGNORE INTO notifications VALUES (?, ?)", [(i, ts) for i in ids])
    con.commit()


def start_run(con: sqlite3.Connection, window_from: str, window_to: str) -> int:
    cur = con.execute("INSERT INTO runs(started_at, window_from, window_to, status) VALUES (?,?,?,'running')",
                      (now_iso(), window_from, window_to))
    con.commit()
    return cur.lastrowid


def finish_run(con: sqlite3.Connection, run_id: int, *, status: str, fetched: int = 0, new: int = 0,
               analyzed: int = 0, notified: int = 0, error: str | None = None) -> None:
    con.execute("UPDATE runs SET finished_at=?, status=?, fetched=?, new=?, analyzed=?, notified=?, error=? "
                "WHERE run_id=?", (now_iso(), status, fetched, new, analyzed, notified, error, run_id))
    con.commit()


# -- affiliations ------------------------------------------------------------

def _affil_where(only_added: bool, since: str | None) -> tuple[str, list]:
    where, params = ["s.paper_id IS NULL"], []
    if only_added:
        where.append("p.added_at IS NOT NULL")
    if since:
        where.append("p.created >= ?")
        params.append(since)
    return " AND ".join(where), params


def pending_affiliations(con: sqlite3.Connection, *, only_added: bool = False, since: str | None = None,
                         limit: int | None = None) -> list[Paper]:
    w, params = _affil_where(only_added, since)
    sql = (f"SELECT p.* FROM papers p LEFT JOIN affil_status s ON s.paper_id = p.id WHERE {w} "
           f"ORDER BY p.created DESC, p.id DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [paper_from_row(r) for r in con.execute(sql, params)]


def count_pending_affiliations(con: sqlite3.Connection, *, since: str | None = None) -> int:
    w, params = _affil_where(False, since)
    return con.execute(f"SELECT count(*) FROM papers p LEFT JOIN affil_status s ON s.paper_id = p.id WHERE {w}",
                       params).fetchone()[0]


def save_affiliations(con: sqlite3.Connection, pid: str, orgs: Sequence[Org], *, source: str | None,
                      status: str = "ok") -> None:
    """Replace a paper's organizations."""
    row = con.execute("SELECT rowid FROM papers WHERE id = ?", (pid,)).fetchone()
    if not row:
        return
    con.execute("DELETE FROM paper_orgs WHERE paper_id = ?", (pid,))
    con.execute("DELETE FROM orgs_fts WHERE rowid = ?", (row[0],))
    con.executemany("INSERT INTO paper_orgs(paper_id, pos, name, unit, domain, authors) VALUES (?,?,?,?,?,?)",
                    [(pid, i, o.name, o.unit, o.domain, "; ".join(o.authors)) for i, o in enumerate(orgs)])
    if orgs:
        text = " ; ".join(" ".join(x for x in (o.name, o.unit, o.domain) if x) for o in orgs)
        con.execute("INSERT INTO orgs_fts(rowid, text) VALUES (?, ?)", (row[0], text))
    con.execute("INSERT OR REPLACE INTO affil_status(paper_id, status, source, extracted_at) VALUES (?,?,?,?)",
                (pid, status, source, now_iso()))
    con.commit()
