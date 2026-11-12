"""Search UI (FastAPI + server-rendered Jinja templates, no JS build)."""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import db
from .config import Settings
from .gemini import PRICES
from .models import ORG_KINDS

HERE = Path(__file__).parent
PAGE_SIZE = 25
COUNT_CAP = 10000
SORTS = {"best": "Best match", "relevance": "Gemini relevance", "importance": "Gemini importance",
         "newest": "Newest"}
HL_OPEN, HL_CLOSE = "\x02", "\x03"


def fts_query(q: str, strict: bool) -> str:
    """Raw FTS5 syntax if it parses; otherwise every token quoted (implicit AND)."""
    if not strict:
        return q
    toks = [t for t in re.findall(r"\w[\w.\-]*", q) if t not in ("AND", "OR", "NOT", "NEAR")]
    return " ".join(f'"{t}"' for t in toks) or '""'


def author_query(author: str) -> str:
    """FTS5 prefilter on the authors column: every name part longer than an initial must occur.
    db.author_match then checks the parts belong to one author, in order."""
    toks = db.name_tokens(author)
    full = [f'"{t}"' for t in toks if len(t) > 1] or [f'"{t}"' for t in toks]
    if len(full) > 1:  # parts of one name sit within a few tokens of each other (middle names)
        return f'authors : NEAR({" ".join(full)}, 3)'
    return f"authors : {full[0]}" if full else ""


def highlight(s: str | None) -> Markup:
    return Markup(html.escape(s or "").replace(HL_OPEN, "<mark>").replace(HL_CLOSE, "</mark>"))


def _int_or_none(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def search(con: sqlite3.Connection, *, q: str = "", cat: str = "", since: str = "", min_rel: int | None = None,
           min_imp: int | None = None, days: int | None = None, sort: str = "best", page: int = 1,
           author: str = "", org: str = "", kind: str = ""):
    where, params = [], []
    aq = author_query(author)
    if q or aq:
        frm = "papers_fts f JOIN papers p ON p.rowid = f.rowid LEFT JOIN analysis a ON a.id = p.id"
        where.append("papers_fts MATCH ?")
        params.append(None)  # filled per attempt
        snippet = f"snippet(papers_fts, 1, '{HL_OPEN}', '{HL_CLOSE}', '…', 40)" if q else "NULL"
    else:
        frm = "papers p LEFT JOIN analysis a ON a.id = p.id"
        snippet = "NULL"
    if aq and len(db.name_tokens(author)) > 1:  # a single name part: the FTS match is already exact
        where.append("author_match(p.authors, ?)")
        params.append(author)
    if org:
        where.append("p.rowid IN (SELECT rowid FROM orgs_fts WHERE orgs_fts MATCH ?)")
        params.append(fts_query(org, strict=True))
    if kind in ORG_KINDS:
        where.append("p.id IN (SELECT paper_id FROM paper_orgs WHERE kind = ?)")
        params.append(kind)
    if cat:
        where.append("(' ' || p.categories || ' ') LIKE ?")
        params.append(f"% {cat} %")
    if since:
        where.append("p.created >= ?")
        params.append(since)
    if min_rel is not None:
        where.append("a.relevance >= ?")
        params.append(min_rel)
    if min_imp is not None:
        where.append("a.importance >= ?")
        params.append(min_imp)
    if days:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).replace(microsecond=0).isoformat()
        where.append("p.added_at >= ?")
        params.append(cutoff)
    if sort == "best" and not q:
        sort = "relevance" if (days or min_rel is not None) else "newest"
    order = {
        "best": "bm25(papers_fts, 3.0, 1.0, 0.5)",
        "relevance": "a.relevance DESC NULLS LAST, a.importance DESC NULLS LAST, p.created DESC",
        "importance": "a.importance DESC NULLS LAST, a.relevance DESC NULLS LAST, p.created DESC",
        "newest": "p.created DESC, p.id DESC",
    }.get(sort, "p.created DESC")
    w = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (f"SELECT p.*, a.relevance, a.importance, a.topics, a.tldr, a.reason, {snippet} AS snip "
           f"FROM {frm} {w} ORDER BY {order} LIMIT {PAGE_SIZE} OFFSET {(max(page, 1) - 1) * PAGE_SIZE}")
    count_sql = f"SELECT count(*) FROM (SELECT 1 FROM {frm} {w} LIMIT {COUNT_CAP + 1})"
    def match(strict: bool) -> str:
        if not q:
            return aq
        return f"({fts_query(q, strict)}) AND {aq}" if aq else fts_query(q, strict)

    for strict in (False, True):
        p = [match(strict) if x is None else x for x in params]
        try:
            rows = con.execute(sql, p).fetchall()
            total = con.execute(count_sql, p).fetchone()[0]
            return rows, total, sort, None
        except sqlite3.OperationalError as e:
            if not q or strict:
                return [], 0, sort, str(e)
    return [], 0, sort, None


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="arXiv search", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["hl"] = highlight
    templates.env.filters["topics"] = lambda s: json.loads(s) if s else []
    templates.env.filters["num"] = lambda n: f"{n:,}" if isinstance(n, int) else n
    templates.env.globals["categories"] = settings.categories
    templates.env.globals["org_kinds"] = ORG_KINDS
    templates.env.filters["authors"] = lambda s: s.split("; ") if s else []

    def conn() -> sqlite3.Connection:
        return db.connect(settings.db_path)

    c = conn()
    try:
        db.init_db(c)
    finally:
        c.close()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, q: str = "", cat: str = "", since: str = "", min_rel: str = "",
              min_imp: str = "", days: str = "", sort: str = "best", page: int = 1,
              author: str = "", org: str = "", kind: str = ""):
        q, author, org = q.strip(), author.strip(), org.strip()
        kind = kind if kind in ORG_KINDS else ""
        f = dict(q=q, cat=cat, since=since, min_rel=_int_or_none(min_rel), min_imp=_int_or_none(min_imp),
                 days=_int_or_none(days), sort=sort if sort in SORTS else "best", author=author, org=org, kind=kind)
        form = dict(f)  # what the user asked for; the form shows this, not the landing default
        if not any((q, cat, since, f["min_rel"] is not None, f["min_imp"] is not None, f["days"], author, org,
                    kind)):
            f["days"] = 7  # landing page: what the updater found this week
        con = conn()
        try:
            rows, total, used_sort, error = search(con, page=page, **f)
            new_count = con.execute("SELECT count(*) FROM papers WHERE added_at IS NOT NULL").fetchone()[0]
            orgs = db.orgs_for(con, [r["id"] for r in rows])
            n_affil = con.execute("SELECT count(*) FROM affil_status").fetchone()[0] if (org or kind) else None
        finally:
            con.close()
        base = {k: v for k, v in dict(q=q, author=author, org=org, kind=kind, cat=cat, since=since, min_rel=min_rel,
                                         min_imp=min_imp, days=f["days"] or "", sort=sort).items()
                if v not in ("", None)}
        pages = (min(total, COUNT_CAP) + PAGE_SIZE - 1) // PAGE_SIZE
        link = lambda n: "/?" + urlencode({**base, "page": n})
        return templates.TemplateResponse(request, "index.html", dict(
            rows=rows, total=min(total, COUNT_CAP), capped=total > COUNT_CAP, f=f, form=form, sort=used_sort, sorts=SORTS, page=page,
            pages=pages, prev_url=link(page - 1) if page > 1 else None,
            next_url=link(page + 1) if page < pages else None, error=error, new_count=new_count, orgs=orgs,
            n_affil=n_affil))

    @app.get("/paper/{pid:path}", response_class=HTMLResponse)
    def paper(request: Request, pid: str):
        con = conn()
        try:
            r = con.execute("SELECT p.*, a.relevance, a.importance, a.topics, a.tldr, a.reason, a.model, "
                            "a.analyzed_at FROM papers p LEFT JOIN analysis a ON a.id = p.id WHERE p.id = ?",
                            (pid,)).fetchone()
            orgs = db.orgs_for(con, [pid]).get(pid, [])
            affil = con.execute("SELECT * FROM affil_status WHERE paper_id = ?", (pid,)).fetchone()
        finally:
            con.close()
        if not r:
            raise HTTPException(404, "paper not in index")
        return templates.TemplateResponse(request, "paper.html", {"r": r, "orgs": orgs, "affil": affil})

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request):
        con = conn()
        try:
            one = lambda q: con.execute(q).fetchone()[0]
            s = dict(papers=one("SELECT count(*) FROM papers"), analysed=one("SELECT count(*) FROM analysis"),
                     discovered=one("SELECT count(*) FROM papers WHERE added_at IS NOT NULL"),
                     newest=one("SELECT max(created) FROM papers"),
                     notified=one("SELECT count(*) FROM notifications"),
                     affiliated=one("SELECT count(DISTINCT paper_id) FROM paper_orgs"))
            top_orgs = con.execute("SELECT name, kind, count(DISTINCT paper_id) AS n FROM paper_orgs "
                                   "GROUP BY name ORDER BY n DESC, name LIMIT 30").fetchall()
            runs = con.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 20").fetchall()
            usage = []
            for u in con.execute("SELECT * FROM usage ORDER BY day DESC, model LIMIT 60"):
                pin, pout = PRICES.get(u["model"], (None, None))
                cost = (u["input_tokens"] * pin + u["output_tokens"] * pout) / 1e6 if pin is not None else None
                usage.append({**dict(u), "cost": cost})
        finally:
            con.close()
        return templates.TemplateResponse(request, "stats.html", dict(s=s, runs=runs, usage=usage, top_orgs=top_orgs,
                                                                        model=settings.gemini_model))

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        con = conn()
        try:
            con.execute("SELECT 1").fetchone()
        finally:
            con.close()
        return "ok"

    return app
