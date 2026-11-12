"""Web UI: search with judgment buttons, papers by affiliation (newest first), stats."""
from __future__ import annotations

import collections
import datetime as dt
import html
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import db
from .config import Settings

HERE = Path(__file__).parent
PAGE_SIZE = 25
LABELS = ("interesting", "not interesting")
HL_OPEN, HL_CLOSE = "\x02", "\x03"


def fts_query(q: str, strict: bool) -> str:
    """Raw FTS5 syntax if it parses; otherwise every token quoted (implicit AND)."""
    if not strict:
        return q
    toks = [t for t in re.findall(r"\w[\w.\-]*", q) if t not in ("AND", "OR", "NOT", "NEAR")]
    return " ".join(f'"{t}"' for t in toks) or '""'


def highlight(s: str | None) -> Markup:
    return Markup(html.escape(s or "").replace(HL_OPEN, "<mark>").replace(HL_CLOSE, "</mark>"))


def _page(con: sqlite3.Connection, frm: str, where: str, order: str, param: str | None, page: int, strict: bool,
          snippet: str = "NULL"):
    p = [fts_query(param, strict)] if param is not None else []
    rows = con.execute(f"SELECT p.*, {snippet} AS snip FROM {frm} {where} ORDER BY {order} "
                       f"LIMIT {PAGE_SIZE + 1} OFFSET {(max(page, 1) - 1) * PAGE_SIZE}", p).fetchall()
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def query(con: sqlite3.Connection, frm: str, where: str, order: str, q: str, page: int, snippet: str = "NULL"):
    """Run an FTS query, retrying with every token quoted if the raw syntax does not parse."""
    for strict in (False, True):
        try:
            return (*_page(con, frm, where, order, q, page, strict, snippet), None)
        except sqlite3.OperationalError as e:
            if strict:
                return [], False, str(e)


def search_papers(con: sqlite3.Connection, q: str, org: str, page: int):
    """Papers matching the text query (best match first; newest first without one), optionally only
    those with affiliation `org`."""
    only_org = "rowid IN (SELECT p.rowid FROM paper_orgs o JOIN papers p ON p.id = o.paper_id WHERE o.name = ?)"
    for strict in (False, True):
        if q:  # rank inside the FTS index, then join only the rows shown
            ranked = (f"SELECT rowid, bm25(papers_fts, 3.0, 1.0, 0.5) AS r, "
                      f"snippet(papers_fts, 1, '{HL_OPEN}', '{HL_CLOSE}', '…', 40) AS snip FROM papers_fts "
                      f"WHERE papers_fts MATCH ?{' AND ' + only_org if org else ''} ORDER BY r")
            params = [fts_query(q, strict)] + ([org] if org else [])
        else:
            ranked = (f"SELECT rowid, NULL AS snip FROM papers{' WHERE ' + only_org if org else ''} "
                      f"ORDER BY created DESC, id DESC")
            params = [org] if org else []
        try:
            rows = con.execute(f"SELECT p.*, m.snip FROM ({ranked} LIMIT {PAGE_SIZE + 1} OFFSET "
                               f"{(max(page, 1) - 1) * PAGE_SIZE}) m JOIN papers p ON p.rowid = m.rowid "
                               f"ORDER BY {'m.r' if q else 'p.created DESC, p.id DESC'}", params).fetchall()
        except sqlite3.OperationalError as e:
            if strict:
                return [], False, str(e)
            continue
        return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE, None


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="arXiv search", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["hl"] = highlight
    templates.env.globals["labels"] = LABELS

    def conn() -> sqlite3.Connection:
        return db.connect(settings.db_path)

    c = conn()
    try:
        db.init_db(c)
    finally:
        c.close()

    def render(request: Request, name: str, con: sqlite3.Connection, rows, **ctx):
        ids = [r["id"] for r in rows]
        return templates.TemplateResponse(request, name, dict(
            rows=rows, orgs=db.orgs_for(con, ids), user_labels=db.labels_for(con, ids),
            here=str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""), **ctx))

    def pager(base: dict, page: int, more: bool):
        link = lambda n: "?" + urlencode({**base, "page": n})
        return dict(page=page, prev_url=link(page - 1) if page > 1 else None, next_url=link(page + 1) if more else None)

    @app.get("/", response_class=HTMLResponse)
    def search(request: Request, q: str = "", org: str = "", page: int = 1):
        q, org = q.strip(), org.strip()
        con = conn()
        try:
            rows, more, error = search_papers(con, q, org, page)
            return render(request, "index.html", con, rows, q=q, org=org, error=error, page_kind="search",
                          **pager({k: v for k, v in (("q", q), ("org", org)) if v}, page, more))
        finally:
            con.close()

    @app.get("/affiliation", response_class=HTMLResponse)
    def affiliation(request: Request, q: str = "", page: int = 1):
        q = q.strip()
        con = conn()
        try:
            rows, more, error = (query(con, "orgs_fts o JOIN papers p ON p.rowid = o.rowid", "WHERE orgs_fts MATCH ?",
                                       "p.created DESC, p.id DESC", q, page) if q else ([], False, None))
            return render(request, "affiliation.html", con, rows, q=q, org="", error=error, page_kind="affiliation",
                          **pager({"q": q}, page, more))
        finally:
            con.close()

    @app.post("/label")
    def label(id: str = Form(...), label: str = Form(""), next: str = Form("/")):
        con = conn()
        try:
            db.set_label(con, id, label if label in LABELS else None)
        finally:
            con.close()
        return RedirectResponse(next if next.startswith("/") else "/", status_code=303)

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request, days: int = 30):
        cutoff = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=max(days, 1))).isoformat()
        con = conn()
        try:
            authors = collections.Counter(
                a for (s,) in con.execute("SELECT authors FROM papers WHERE created >= ?", (cutoff,)) if s
                for a in s.split("; ")).most_common(50)
            orgs = con.execute(
                "SELECT o.name, count(DISTINCT o.paper_id) AS n FROM paper_orgs o JOIN papers p ON p.id = o.paper_id "
                "WHERE p.created >= ? GROUP BY o.name ORDER BY n DESC, o.name LIMIT 50", (cutoff,)).fetchall()
        finally:
            con.close()
        return templates.TemplateResponse(request, "stats.html", dict(days=days, authors=authors, orgs=orgs))

    return app
