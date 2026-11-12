"""Fetch newly submitted papers from the arXiv API (export.arxiv.org/api/query).

The API is arXiv's documented programmatic interface. Terms of use: at most
one request every 3 seconds, one connection at a time. (OAI-PMH list requests
were timing out at arXiv's CDN when this was written, so we use the API.)
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Iterator, Sequence

import httpx

from . import USER_AGENT
from .ratelimit import RateLimit
from .models import Paper, norm

log = logging.getLogger(__name__)

API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom",
      "os": "http://a9.com/-/spec/opensearch/1.1/"}
MIN_GAP = 3.5  # seconds between requests


class ArxivApiError(RuntimeError):
    pass


def build_query(categories: Sequence[str], start: dt.date, end: dt.date) -> str:
    cats = " OR ".join(f"cat:{c}" for c in categories)
    return f"({cats}) AND submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]"


def _id_from_url(url: str) -> str:
    return re.sub(r"v\d+$", "", url.split("/abs/", 1)[-1].strip())


def parse_feed(xml: bytes | str) -> tuple[int, list[Paper]]:
    root = ET.fromstring(xml)
    entries = root.findall("a:entry", NS)
    if len(entries) == 1 and "/api/errors" in (entries[0].findtext("a:id", "", NS)):
        raise ArxivApiError(norm(entries[0].findtext("a:summary", "", NS)) or "arXiv API error")
    total = int(root.findtext("os:totalResults", "0", NS) or 0)
    papers = []
    for e in entries:
        pid = _id_from_url(e.findtext("a:id", "", NS))
        if not pid:
            continue
        pc = e.find("arxiv:primary_category", NS)
        cats = [c.get("term") for c in e.findall("a:category", NS) if c.get("term")]
        if pc is not None and pc.get("term"):
            cats = [pc.get("term")] + [c for c in cats if c != pc.get("term")]
        published = (e.findtext("a:published", "", NS) or "")[:10] or None
        updated = (e.findtext("a:updated", "", NS) or "")[:10] or None
        papers.append(Paper(
            id=pid,
            title=norm(e.findtext("a:title", "", NS)) or "",
            abstract=norm(e.findtext("a:summary", "", NS)) or "",
            authors=[norm(a.findtext("a:name", "", NS)) for a in e.findall("a:author", NS)
                     if a.findtext("a:name", "", NS)],
            categories=list(dict.fromkeys(cats)),
            created=published,
            updated=updated if updated and updated != published else None,
            doi=norm(e.findtext("arxiv:doi", None, NS)),
            journal_ref=norm(e.findtext("arxiv:journal_ref", None, NS)),
            comments=norm(e.findtext("arxiv:comment", None, NS)),
            datestamp=updated,
        ))
    return total, papers


class ArxivClient:
    def __init__(self, client: httpx.Client | None = None, min_gap: float = MIN_GAP, max_retries: int = 6,
                 lock_path: str | None = None):
        self.http = client or httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT})
        self.limit = RateLimit(min_gap, lock_path)  # shared with other arxiv-search processes
        self.max_retries = max_retries

    def _get(self, params: dict) -> bytes:
        for attempt in range(1, self.max_retries + 1):
            self.limit.wait()
            try:
                r = self.http.get(API, params=params)
                if r.status_code == 200:
                    return r.content
                delay = (300 if r.status_code == 429 else 30) * attempt
                ra = r.headers.get("Retry-After", "")
                if ra.isdigit():
                    delay = max(delay, int(ra))
                log.warning("arXiv API HTTP %s, retry %d/%d in %ds", r.status_code, attempt, self.max_retries, delay)
            except httpx.HTTPError as e:
                delay = 30 * attempt
                log.warning("arXiv API %s: %s, retry %d/%d in %ds", type(e).__name__, e, attempt,
                            self.max_retries, delay)
            time.sleep(min(delay, 1800))
        raise ArxivApiError(f"arXiv API failed after {self.max_retries} attempts")

    def fetch_submitted(self, categories: Sequence[str], start: dt.date, end: dt.date,
                        page_size: int = 200) -> Iterator[Paper]:
        """Yield all papers in `categories` first submitted between start and end (inclusive, UTC)."""
        query = build_query(categories, start, end)
        offset, total, empty_retries = 0, None, 0
        while total is None or offset < total:
            params = {"search_query": query, "start": offset, "max_results": page_size,
                      "sortBy": "submittedDate", "sortOrder": "ascending"}
            try:
                total, papers = parse_feed(self._get(params))
            except ArxivApiError as e:
                if empty_retries >= 3:
                    raise
                empty_retries += 1
                log.warning("arXiv API error page (%s); retrying", e)
                time.sleep(30)
                continue
            if not papers:
                # The API occasionally returns an empty page mid-result-set; retry a few times.
                if offset < total and empty_retries < 3:
                    empty_retries += 1
                    log.warning("empty page at offset %d/%d, retrying", offset, total)
                    time.sleep(10)
                    continue
                break
            empty_retries = 0
            log.info("arXiv API: %d-%d of %d", offset + 1, offset + len(papers), total)
            yield from papers
            offset += len(papers)
