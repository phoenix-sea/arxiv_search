"""Telegram Bot API notifications (sendMessage, HTML parse mode)."""
from __future__ import annotations

import html
import logging
import time

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096


class TelegramError(RuntimeError):
    pass


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


class Telegram:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None, min_gap: float = 1.1):
        self.token, self.chat_id = token, chat_id
        self.http = client or httpx.Client(timeout=30)
        self.min_gap = min_gap
        self._last = 0.0

    def _call(self, method: str, payload: dict | None = None) -> dict:
        url = API.format(token=self.token, method=method)
        for attempt in range(1, 6):
            wait = self.min_gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.http.post(url, json=payload or {})
            except httpx.HTTPError as e:
                log.warning("Telegram %s, retry %d/5", type(e).__name__, attempt)
                time.sleep(5 * attempt)
                continue
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 429:
                time.sleep(int((data.get("parameters") or {}).get("retry_after", 5)) + 1)
                continue
            if not data.get("ok"):
                raise TelegramError(f"Telegram {method} failed: HTTP {r.status_code} {data.get('description', r.text[:200])}")
            return data
        raise TelegramError(f"Telegram {method} failed after retries")

    def send(self, text: str) -> None:
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN - 1] + "…"
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                   "link_preview_options": {"is_disabled": True}})


def format_paper(p, orgs: list[str]) -> str:
    """Title, authors, affiliations, date and the arXiv link."""
    authors = ", ".join(p.authors[:5]) + (f" +{len(p.authors) - 5}" if len(p.authors) > 5 else "")
    lines = [f"<b>{esc(p.title)}</b>", esc(authors)]
    if orgs:
        lines.append("🏢 " + esc("; ".join(orgs)))
    lines.append(f"{esc(p.created)} · {esc(' '.join(p.categories))}")
    lines.append(f'<a href="https://arxiv.org/abs/{html.escape(p.id)}">arxiv.org/abs/{esc(p.id)}</a>')
    return "\n".join(lines)
