"""Command line: arxiv-search <command> --help"""
from __future__ import annotations

import argparse
import logging

from . import db
from .config import load_settings


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
    from .gemini import Gemini
    from .pipeline import run_update
    from .telegram import Telegram
    con = db.connect(s.db_path)
    db.init_db(con)
    run_update(con, s.categories, fetcher=ArxivClient().fetch_submitted, headers=HeaderFetcher(),
               gemini=Gemini(s.gemini_api_key, s.gemini_model) if s.gemini_api_key and not a.no_analyze else None,
               telegram=Telegram(s.telegram_bot_token, s.telegram_chat_id)
               if s.telegram_bot_token and s.telegram_chat_id and not a.no_notify else None)


def cmd_serve(s, a):
    import uvicorn
    from .web import create_app
    uvicorn.run(create_app(s), host=a.host, port=a.port or s.web_port, log_level="info")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arxiv-search", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create or migrate the database").set_defaults(f=cmd_init)
    p = sub.add_parser("import-kaggle", help="initial index from the Kaggle arXiv snapshot (.zip or .json)")
    p.add_argument("path"); p.set_defaults(f=cmd_import_kaggle)
    p = sub.add_parser("update", help="daily job: crawl, affiliations, Gemini, Telegram, backfill")
    p.add_argument("--no-analyze", action="store_true")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(f=cmd_update)
    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int); p.set_defaults(f=cmd_serve)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.f(load_settings(), a)


if __name__ == "__main__":
    main()
