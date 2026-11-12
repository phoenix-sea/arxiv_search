"""Daily update: fetch new papers -> store -> Gemini analysis -> Telegram -> affiliations."""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from typing import Callable, Iterable, Sequence

from . import db
from .affiliations import FetchError, HeaderFetcher, read_affiliations
from .config import Settings
from .gemini import GeminiAnalyzer, GeminiError
from .models import Paper
from .orgparse import PARSER_VERSION
from .telegram import Telegram, TelegramError, esc, format_digest, format_paper

log = logging.getLogger(__name__)

Fetcher = Callable[[Sequence[str], dt.date, dt.date], Iterable[Paper]]


def make_analyzer(settings: Settings, model: str | None = None) -> GeminiAnalyzer | None:
    if not settings.gemini_enabled:
        return None
    return GeminiAnalyzer(settings.gemini_api_key, model or settings.gemini_model, settings.profile,
                          thinking_level=settings.gemini_thinking_level, rpm=settings.gemini_rpm)


def make_notifier(settings: Settings) -> Telegram | None:
    return Telegram(settings.telegram_bot_token, settings.telegram_chat_id) if settings.telegram_enabled else None


def analyze_papers(con: sqlite3.Connection, analyzer: GeminiAnalyzer, papers: Sequence[Paper],
                   batch_size: int) -> int:
    """Analyse and store. Stops early (keeping progress) if Gemini keeps failing,
    so remaining papers are picked up by the next run."""
    done = 0
    for i in range(0, len(papers), batch_size):
        batch = papers[i:i + batch_size]
        try:
            res = analyzer.analyze(batch)
        except GeminiError as e:
            log.error("Gemini failed (%s); %d papers left for the next run", e, len(papers) - i)
            if done == 0:
                raise
            break
        db.save_assessments(con, res.assessments, analyzer.model)
        db.record_usage(con, analyzer.model, res.calls, res.input_tokens, res.output_tokens)
        done += len(res.assessments)
        if (i // batch_size) % 10 == 0 or i + batch_size >= len(papers):
            log.info("analysed %d/%d", done, len(papers))
    return done


def extract_affiliations(con: sqlite3.Connection, fetcher: HeaderFetcher, papers: Sequence[Paper]) -> int:
    """Download each paper's author block from arXiv and parse its organizations (rules, no LLM).
    Returns papers processed. Stops early, keeping progress, if arXiv keeps failing."""
    done, failures = 0, 0
    for i, p in enumerate(papers, 1):
        try:
            got = read_affiliations(fetcher, p.id, p.authors)
            failures = 0
        except FetchError as e:
            failures += 1
            log.warning("%s (%d in a row)", e, failures)
            if failures >= 3:
                log.error("arXiv keeps failing; %d papers left for the next run", len(papers) - i)
                break
            continue
        if got:
            source, header, orgs = got
            db.save_affiliations(con, p.id, orgs, source=source, header=header, parser=PARSER_VERSION)
        else:
            db.save_affiliations(con, p.id, [], source=None, header=None, parser=PARSER_VERSION, status="no_source")
        done += 1
        if i % 100 == 0:
            log.info("affiliations: %d/%d", i, len(papers))
    return done


def send_notifications(con: sqlite3.Connection, settings: Settings, notifier: Telegram, n_new: int,
                       n_analyzed: int) -> int:
    cands = db.notification_candidates(con, settings.notify_min_relevance, settings.notify_min_importance)
    if n_new == 0 and not cands:
        return 0
    top = cands[:settings.notify_max_per_run]
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    notifier.send(format_digest(day, n_new, n_analyzed, len(cands), top, settings.notify_min_relevance,
                                settings.public_url))
    sent = 0
    for row in top:
        notifier.send(format_paper(row, settings.public_url))
        db.mark_notified(con, [row["id"]])
        sent += 1
    db.mark_notified(con, [r["id"] for r in cands])  # overflow is listed in the digest/UI
    return sent


def run_update(settings: Settings, con: sqlite3.Connection, *, fetcher: Fetcher,
               analyzer: GeminiAnalyzer | None, notifier: Telegram | None,
               affiliations: HeaderFetcher | None = None,
               since: dt.date | None = None, until: dt.date | None = None) -> dict:
    today = dt.datetime.now(dt.timezone.utc).date()
    end = until or today
    if since is None:
        mc = db.max_created(con)
        since = (dt.date.fromisoformat(mc) if mc else today) - dt.timedelta(days=settings.lookback_days)
    run_id = db.start_run(con, since.isoformat(), end.isoformat())
    stats = {"fetched": 0, "new": 0, "analyzed": 0, "notified": 0, "affiliated": 0}
    log.info("update window %s .. %s, categories %s", since, end, ",".join(settings.categories))
    try:
        papers = list(fetcher(settings.categories, since, end))
        stats["fetched"] = len(papers)
        new_ids = db.upsert_papers(con, papers, added_at=db.now_iso())
        stats["new"] = len(new_ids)
        log.info("fetched %d papers, %d new", len(papers), len(new_ids))

        if analyzer:
            pending = db.pending_analysis(con, only_added=True)
            if pending:
                stats["analyzed"] = analyze_papers(con, analyzer, pending, settings.gemini_batch_size)
        else:
            log.warning("GEMINI_API_KEY not set: skipping analysis")

        if notifier:
            stats["notified"] = send_notifications(con, settings, notifier, stats["new"], stats["analyzed"])
        else:
            log.info("Telegram not configured: skipping notifications")

        # last, because it is slow (one arXiv request per paper) and must not delay notifications
        status, error = "ok", None
        if affiliations:
            pending = db.pending_affiliations(con, only_added=True)
            if pending:
                log.info("extracting affiliations for %d papers (~%d min)", len(pending), len(pending) * 4 // 60)
                try:
                    stats["affiliated"] = extract_affiliations(con, affiliations, pending)
                except Exception as e:  # papers and notifications are done; retry the rest next run
                    log.exception("affiliation extraction failed")
                    status, error = "partial", f"affiliations: {type(e).__name__}: {e}"[:1000]
        db.finish_run(con, run_id, status=status, error=error, **stats)
        log.info("update done: %s", stats)
        return stats
    except Exception as e:
        db.finish_run(con, run_id, status="error", error=f"{type(e).__name__}: {e}"[:1000], **stats)
        if notifier:
            try:
                notifier.send(f"⚠️ <b>arXiv update failed</b>\n<code>{esc(f'{type(e).__name__}: {e}'[:500])}</code>")
            except TelegramError:
                log.exception("could not send failure notice")
        raise
