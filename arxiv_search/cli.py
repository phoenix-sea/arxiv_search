"""Command line: arxiv-search <command> --help"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from . import db
from .config import load_settings


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _arxiv_lock(s) -> str:
    """Shared arXiv rate-limit file: the daily update and a backfill may run at the same time."""
    return str(s.db_path.parent / ".arxiv-ratelimit")


def cmd_init(s, a):
    con = db.connect(s.db_path)
    db.init_db(con)
    n = con.execute("SELECT count(*) FROM papers").fetchone()[0]
    print(f"database ready: {s.db_path} ({n:,} papers)")


def cmd_import_kaggle(s, a):
    from .kaggle import import_snapshot
    con = db.connect(s.db_path)
    db.init_db(con)
    n = import_snapshot(con, a.path, s.categories)
    print(f"imported {n:,} papers into {s.db_path}")


def cmd_update(s, a):
    from .affiliations import HeaderFetcher
    from .arxiv_api import ArxivClient
    from .pipeline import make_analyzer, make_notifier, run_update
    con = db.connect(s.db_path)
    db.init_db(con)
    stats = run_update(
        s, con, fetcher=ArxivClient(lock_path=_arxiv_lock(s)).fetch_submitted,
        analyzer=None if a.no_analyze else make_analyzer(s),
        notifier=None if a.no_notify else make_notifier(s),
        affiliations=None if (a.no_affiliations or not s.affiliations) else HeaderFetcher(lock_path=_arxiv_lock(s)),
        since=a.since, until=a.until)
    print(stats)


def cmd_analyze(s, a):
    from .gemini import estimate_cost
    from .pipeline import analyze_papers, make_analyzer
    con = db.connect(s.db_path)
    db.init_db(con)
    since = a.since.isoformat() if a.since else None
    n = db.count_pending(con, since=since)
    if a.limit:
        n = min(n, a.limit)
    model = a.model or s.gemini_model
    cost = estimate_cost(model, n)
    print(f"{n:,} unanalysed papers{' since ' + since if since else ''} · model {model} · "
          f"estimated cost {'$%.2f' % cost if cost is not None else 'unknown'}")
    if n == 0:
        return
    if n > 1000 and not a.yes:
        sys.exit("more than 1,000 papers: re-run with --yes to confirm the spend")
    analyzer = make_analyzer(s, model)
    if analyzer is None:
        sys.exit("GEMINI_API_KEY is not set")
    chunk = 1000  # load in chunks so memory stays small on a Pi
    total = 0
    while total < n:
        papers = db.pending_analysis(con, since=since, limit=min(chunk, n - total))
        if not papers:
            break
        done = analyze_papers(con, analyzer, papers, a.batch_size or s.gemini_batch_size)
        total += done
        if done == 0:  # Gemini is failing; progress so far is saved
            break
    print(f"analysed {total:,} papers")


def cmd_affiliations(s, a):
    from . import affiliations as af
    from .orgparse import PARSER_VERSION, parse_block
    from .pipeline import extract_affiliations
    con = db.connect(s.db_path)
    db.init_db(con)
    if a.reparse:  # re-run the rules on stored author blocks, no downloads
        rows = db.stored_headers(con)
        for r in rows:
            orgs = parse_block(r["header"], r["authors"].split("; ") if r["authors"] else [])
            db.save_affiliations(con, r["paper_id"], orgs, source=r["source"], header=r["header"],
                                 parser=PARSER_VERSION, commit=False)
        con.commit()
        print(f"re-parsed {len(rows):,} stored author blocks with {PARSER_VERSION}")
        return
    since = a.since.isoformat() if a.since else None
    n = db.count_pending_affiliations(con, since=since)
    if a.limit:
        n = min(n, a.limit)
    hours = n * (af.MIN_GAP + 0.5) / 3600
    print(f"{n:,} papers without affiliations{' since ' + since if since else ''} · "
          f"about {hours:.1f} h of arXiv downloads (one request per {af.MIN_GAP:g} s) · no Gemini")
    if n == 0:
        return
    if n > 1000 and not a.yes:
        sys.exit("more than 1,000 papers: re-run with --yes to confirm")
    fetcher = af.HeaderFetcher(lock_path=_arxiv_lock(s))
    total = 0
    while total < n:
        papers = db.pending_affiliations(con, since=since, limit=min(200, n - total))
        if not papers:
            break
        done = extract_affiliations(con, fetcher, papers)
        total += done
        if done == 0:
            break
    print(f"processed {total:,} papers")


def cmd_test_affiliations(s, a):
    from .affiliations import HeaderFetcher, read_affiliations
    con = db.connect(s.db_path)
    db.init_db(con)
    fetcher = HeaderFetcher(lock_path=_arxiv_lock(s))
    for pid in a.ids:
        row = con.execute("SELECT * FROM papers WHERE id = ?", (pid,)).fetchone()
        authors = db.paper_from_row(row).authors if row else []
        print(f"\n{pid}  {row['title'] if row else '(not in index)'}")
        got = read_affiliations(fetcher, pid, authors)
        if not got:
            print("  no HTML or PDF author block found")
            continue
        source, header, orgs = got
        print(f"  author block from {source.upper()}:\n    | " + header.replace("\n", "\n    | "))
        for o in orgs:
            print(f"  -> {o.name}{' / ' + o.unit if o.unit else ''} [{o.kind}]"
                  f"{' ' + o.domain if o.domain else ''}: {'; '.join(o.authors) or '?'}")
        if not orgs:
            print("  -> no affiliations found")


def cmd_serve(s, a):
    import uvicorn
    from .web import create_app
    uvicorn.run(create_app(s), host=a.host or s.web_host, port=a.port or s.web_port, log_level="info")


def cmd_telegram_chat_id(s, a):
    from .telegram import find_chat_ids
    if not s.telegram_bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    chats = find_chat_ids(s.telegram_bot_token)
    if not chats:
        print("No chats yet: open your bot in Telegram, send it any message, then run this again.")
    for cid, desc in chats:
        print(f"TELEGRAM_CHAT_ID={cid}    # {desc}")


def cmd_test_telegram(s, a):
    from .pipeline import make_notifier
    n = make_notifier(s)
    if not n:
        sys.exit("set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first")
    n.send("✅ <b>arxiv-search</b> is connected. You'll get new relevant papers here.")
    print("sent")


def cmd_test_gemini(s, a):
    from .pipeline import make_analyzer
    an = make_analyzer(s)
    if not an:
        sys.exit("GEMINI_API_KEY is not set")
    con = db.connect(s.db_path)
    db.init_db(con)
    rows = con.execute("SELECT * FROM papers ORDER BY created DESC LIMIT ?", (a.n,)).fetchall()
    papers = [db.paper_from_row(r) for r in rows]
    res = an.analyze(papers)
    for p in papers:
        x = next((x for x in res.assessments if x.id == p.id), None)
        print(f"\n{p.id}  {p.title}")
        print(f"  relevance {x.relevance} · importance {x.importance} · {x.topics}\n  {x.tldr}\n  {x.reason}"
              if x else "  (no result)")
    print(f"\n{res.calls} call(s), {res.input_tokens} input / {res.output_tokens} output tokens (not saved)")


def cmd_stats(s, a):
    con = db.connect(s.db_path)
    db.init_db(con)
    one = lambda q: con.execute(q).fetchone()[0]
    print(f"papers {one('SELECT count(*) FROM papers'):,} · analysed {one('SELECT count(*) FROM analysis'):,} · "
          f"with affiliations {one('SELECT count(DISTINCT paper_id) FROM paper_orgs'):,} · "
          f"discovered by updater {one('SELECT count(*) FROM papers WHERE added_at IS NOT NULL'):,} · "
          f"newest {one('SELECT max(created) FROM papers')}")
    for r in con.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 5"):
        print(f"run {r['run_id']} {r['started_at']} {r['status']}: fetched {r['fetched']} new {r['new']} "
              f"analysed {r['analyzed']} notified {r['notified']} {r['error'] or ''}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arxiv-search", description=__doc__)
    ap.add_argument("--env", help="path to .env (default: project .env)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create or migrate the database").set_defaults(f=cmd_init)
    p = sub.add_parser("import-kaggle", help="initial index from the Kaggle arXiv snapshot (.zip or .json)")
    p.add_argument("path"); p.set_defaults(f=cmd_import_kaggle)
    p = sub.add_parser("update", help="fetch new papers, analyse with Gemini, notify (run daily)")
    p.add_argument("--since", type=_date, help="window start YYYY-MM-DD (default: newest paper - LOOKBACK_DAYS)")
    p.add_argument("--until", type=_date)
    p.add_argument("--no-analyze", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--no-affiliations", action="store_true", help="skip author affiliation extraction")
    p.set_defaults(f=cmd_update)
    p = sub.add_parser("analyze", help="backfill Gemini analysis for already-indexed papers")
    p.add_argument("--since", type=_date, help="only papers first submitted on/after this date")
    p.add_argument("--limit", type=int)
    p.add_argument("--model", help="override GEMINI_MODEL (e.g. gemini-2.5-flash-lite for cheap backfill)")
    p.add_argument("--batch-size", type=int)
    p.add_argument("--yes", action="store_true", help="confirm runs over 1,000 papers")
    p.set_defaults(f=cmd_analyze)
    p = sub.add_parser("affiliations", help="backfill author affiliations for already-indexed papers")
    p.add_argument("--since", type=_date, help="only papers first submitted on/after this date")
    p.add_argument("--limit", type=int)
    p.add_argument("--reparse", action="store_true",
                   help="re-run the parsing rules on stored author blocks (no downloads)")
    p.add_argument("--yes", action="store_true", help="confirm runs over 1,000 papers")
    p.set_defaults(f=cmd_affiliations)
    p = sub.add_parser("test-affiliations", help="show a paper's author block and parsed affiliations (not saved)")
    p.add_argument("ids", nargs="+", metavar="ID"); p.set_defaults(f=cmd_test_affiliations)
    p = sub.add_parser("serve", help="run the search web UI")
    p.add_argument("--host"); p.add_argument("--port", type=int); p.set_defaults(f=cmd_serve)
    sub.add_parser("telegram-chat-id", help="show chat ids that messaged your bot").set_defaults(f=cmd_telegram_chat_id)
    sub.add_parser("test-telegram", help="send a test message").set_defaults(f=cmd_test_telegram)
    p = sub.add_parser("test-gemini", help="analyse the N newest papers and print results (not saved)")
    p.add_argument("-n", type=int, default=3); p.set_defaults(f=cmd_test_gemini)
    sub.add_parser("stats", help="database and recent run summary").set_defaults(f=cmd_stats)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.f(load_settings(a.env), a)


if __name__ == "__main__":
    main()
