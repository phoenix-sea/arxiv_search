"""Daily job, in priority order:
  1. crawl papers submitted since the newest one in the index;
  2. parse the new papers' affiliations;
  3. Gemini: is each new paper interesting?
  4. Telegram message for each interesting one;
  5. backfill affiliations of older papers (newest first) until shortly before the next run.
All arXiv traffic is in this one process, one request at a time.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from typing import Callable, Iterable, Sequence

from . import db
from .affiliations import FetchError, HeaderFetcher, read_affiliations
from .gemini import Gemini
from .models import Paper
from .telegram import Telegram, format_paper

log = logging.getLogger(__name__)

Fetcher = Callable[[Sequence[str], dt.date, dt.date], Iterable[Paper]]
NEXT_RUN_UTC = dt.time(3, 0)  # keep in sync with deploy/systemd/arxiv-search-update.timer
STOP_BEFORE = dt.timedelta(minutes=30)


def backfill_deadline(now: dt.datetime) -> dt.datetime:
    nxt = dt.datetime.combine(now.date(), NEXT_RUN_UTC, tzinfo=dt.timezone.utc)
    if nxt <= now + STOP_BEFORE:
        nxt += dt.timedelta(days=1)
    return nxt - STOP_BEFORE


def extract_affiliations(con: sqlite3.Connection, fetcher: HeaderFetcher, papers: Sequence[Paper],
                         deadline: dt.datetime | None = None) -> int:
    """Download each paper's author block and store its affiliations. Stops at `deadline`,
    or if arXiv keeps failing; the rest is picked up later."""
    done = failures = 0
    for p in papers:
        if deadline and dt.datetime.now(dt.timezone.utc) >= deadline:
            break
        try:
            got = read_affiliations(fetcher, p.id, p.authors)
            failures = 0
        except FetchError as e:
            failures += 1
            log.warning("%s (%d in a row)", e, failures)
            if failures >= 3:
                break
            continue
        db.save_affiliations(con, p.id, got[1] if got else [], source=got[0] if got else None,
                             status="ok" if got else "no_source")
        done += 1
    return done


def run_update(con: sqlite3.Connection, categories: Sequence[str], *, fetcher: Fetcher, headers: HeaderFetcher,
               gemini: Gemini | None, telegram: Telegram | None, backfill: bool = True) -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    newest = db.max_created(con)
    since = dt.date.fromisoformat(newest) if newest else today
    papers = list(fetcher(categories, since, today))
    new = db.upsert_papers(con, papers, added_at=db.now_iso())
    log.info("crawled %s..%s: %d papers, %d new", since, today, len(papers), len(new))

    n = extract_affiliations(con, headers, db.pending_affiliations(con, only_added=True))
    log.info("affiliations parsed for %d new papers", n)

    if gemini:
        pending = db.unflagged_new(con)
        flags = gemini.flag(pending)
        db.save_flags(con, flags, gemini.model)
        log.info("Gemini: %d of %d judged, %d interesting", len(flags), len(pending), sum(flags.values()))
    else:
        log.warning("GEMINI_API_KEY not set: no interest check")

    if telegram:
        for p in db.to_notify(con):
            telegram.send(format_paper(p, db.orgs_for(con, [p.id]).get(p.id, [])))
            db.mark_notified(con, p.id)
    else:
        log.warning("Telegram not configured: no messages")

    if backfill:
        deadline = backfill_deadline(dt.datetime.now(dt.timezone.utc))
        log.info("backfilling affiliations until %s", deadline.isoformat(timespec="minutes"))
        total = 0
        while dt.datetime.now(dt.timezone.utc) < deadline:
            batch = db.pending_affiliations(con, only_added=False, limit=200)
            if not batch:
                break
            done = extract_affiliations(con, headers, batch, deadline)
            total += done
            if done == 0:
                break
        log.info("backfilled %d papers", total)
