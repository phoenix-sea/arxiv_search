"""Author affiliations (companies, universities, labs) from each paper's author block.

arXiv metadata has author names but almost never affiliations, so we read the
author block from the paper itself:
  1. arXiv's HTML version (arxiv.org/html/ID): the `ltx_authors` block, read up
     to the abstract, so only the first part of the page is downloaded;
  2. otherwise the PDF: text of page 1 up to "Abstract".
orgparse.parse_block then turns that text into organizations (rules, no LLM).

arXiv asks for at most one request every 3 seconds, so this takes about 3.5 s
per paper (roughly 30 minutes for a day's ~500 new papers). No LLM is involved.
"""
from __future__ import annotations

import html as htmllib
import io
import logging
import re
import time
import httpx

from . import USER_AGENT
from .orgparse import parse_block

log = logging.getLogger(__name__)
logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-but-readable PDFs are common

HTML_URL = "https://arxiv.org/html/{id}"
PDF_URL = "https://arxiv.org/pdf/{id}"
MIN_GAP = 3.5  # seconds between arXiv requests
MAX_HTML_BYTES = 2_000_000  # stop reading once past the author block or this much
MAX_PDF_BYTES = 30_000_000
MAX_HEADER_CHARS = 3000


class FetchError(RuntimeError):
    """arXiv could not be reached (transient): the paper is retried on a later run."""


# -- author block extraction ---------------------------------------------------

_END_MARKERS = ('class="ltx_abstract', 'class="ltx_section', 'class="ltx_para', 'class="ltx_keywords')


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def _tidy(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.splitlines())
    out = "\n".join(line for line in lines if line)
    return re.sub(r" ([,;.])", r"\1", out)[:MAX_HEADER_CHARS]


def header_from_html(page: str) -> str | None:
    """Text of the `ltx_authors` block of an arXiv HTML (LaTeXML) page, or None."""
    start = page.find('class="ltx_authors"')
    if start < 0:
        return None
    ends = [i for i in (page.find(m, start) for m in _END_MARKERS) if i > 0]
    end = page.rfind("<", start, min(ends)) if ends else start + 20000  # cut before the marker's tag
    chunk = page[start:end]
    chunk = chunk.split(">", 1)[-1]  # drop the rest of the opening tag
    chunk = re.sub(r'<math[^>]*?alttext="([^"]*)"[^>]*>.*?</math>', r" \1 ", chunk, flags=re.S)
    chunk = re.sub(r"<sup[^>]*>(.*?)</sup>", lambda m: "^" + "".join(_strip_tags(m.group(1)).split()) + " ",
                   chunk, flags=re.S)
    chunk = re.sub(r"<br[^>]*>|<span class=\"ltx_author_before\"[^>]*>|</p>|</div>", "\n", chunk)
    chunk = re.sub(r'<span class="ltx_creator[^"]*"[^>]*>', "\n", chunk)
    text = _tidy(htmllib.unescape(_strip_tags(chunk)))
    return text or None


def header_from_pdf_text(text: str) -> str | None:
    """First-page text up to the abstract."""
    m = re.search(r"\babstract\b", text[20:], re.I)
    head = text[:20 + m.start()] if m else text[:MAX_HEADER_CHARS]
    head = _tidy(head)
    return head if len(head) > 20 else None


def header_from_pdf(data: bytes) -> str | None:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = reader.pages[0].extract_text() if reader.pages else ""
    except Exception as e:  # malformed PDFs raise all sorts of things
        log.info("PDF text extraction failed: %s", e)
        return None
    return header_from_pdf_text(text or "")


class HeaderFetcher:
    """Fetches the author block of a paper from arXiv, one request at a time."""

    def __init__(self, client: httpx.Client | None = None, min_gap: float = MIN_GAP, max_retries: int = 4):
        self.http = client or httpx.Client(timeout=120, follow_redirects=True,
                                           headers={"User-Agent": USER_AGENT})
        self.min_gap = min_gap
        self._last = 0.0
        self.max_retries = max_retries

    def html(self, pid: str) -> str | None:
        """Author block from arXiv's HTML version, or None if there is none."""
        status, body = self._get(HTML_URL.format(id=pid), MAX_HTML_BYTES, stop_at=b'class="ltx_abstract')
        return header_from_html(body.decode("utf-8", "replace")) if status == 200 else None

    def pdf(self, pid: str) -> str | None:
        """Author block from page 1 of the PDF, or None."""
        status, body = self._get(PDF_URL.format(id=pid), MAX_PDF_BYTES)
        return header_from_pdf(body) if status == 200 and body[:5] == b"%PDF-" else None

    def _get(self, url: str, max_bytes: int, stop_at: bytes | None = None) -> tuple[int, bytes]:
        for attempt in range(1, self.max_retries + 1):
            wait = self.min_gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                with self.http.stream("GET", url) as r:
                    if r.status_code in (429, 500, 502, 503, 504):
                        ra = r.headers.get("Retry-After", "")
                        delay = max(int(ra) if ra.isdigit() else 0, (60 if r.status_code == 429 else 15) * attempt)
                        log.warning("arXiv %s HTTP %s, retry %d/%d in %ds", url, r.status_code, attempt,
                                    self.max_retries, delay)
                        time.sleep(min(delay, 900))
                        continue
                    if r.status_code != 200:
                        return r.status_code, b""
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf += chunk
                        # the end marker comes after the author block: no need for the rest
                        if (stop_at and stop_at in buf[-len(chunk) - len(stop_at):]) or len(buf) >= max_bytes:
                            break
                    return 200, bytes(buf)
            except httpx.HTTPError as e:
                delay = 15 * attempt
                log.warning("arXiv %s: %s, retry %d/%d in %ds", url, type(e).__name__, attempt,
                            self.max_retries, delay)
                time.sleep(delay)
        raise FetchError(f"arXiv unreachable for {url}")


def read_affiliations(fetcher: HeaderFetcher, pid: str, authors: list[str]) -> tuple[str, list] | None:
    """(source, orgs) for a paper, or None if arXiv has neither HTML nor PDF text.
    The PDF is also tried when the HTML block names no organization: arXiv's HTML conversion
    sometimes keeps only the names."""
    html = fetcher.html(pid)
    orgs = parse_block(html, authors) if html else []
    if orgs:
        return "html", orgs
    pdf = fetcher.pdf(pid)
    if pdf:
        return "pdf", parse_block(pdf, authors)
    return ("html", []) if html else None
