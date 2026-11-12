"""Telegram Bot API notifications (sendMessage, HTML parse mode)."""
from __future__ import annotations

import html
import json
import logging
import time
from typing import Mapping, Sequence

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


def find_chat_ids(token: str, client: httpx.Client | None = None) -> list[tuple[str, str]]:
    """Chats that recently messaged the bot: [(chat_id, description)]."""
    data = Telegram(token, "", client=client)._call("getUpdates")
    seen: dict[str, str] = {}
    for u in data.get("result", []):
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
            seen[str(chat["id"])] = f"{chat.get('type', '?')}: {name or chat.get('username', '')}"
    return list(seen.items())


def _topics(row: Mapping) -> list[str]:
    try:
        return json.loads(row["topics"] or "[]")
    except (ValueError, TypeError):
        return []


def format_paper(row: Mapping, public_url: str) -> str:
    authors = (row["authors"] or "").split("; ")
    who = authors[0] + (" et al." if len(authors) > 1 else "") if authors and authors[0] else ""
    pid = row["id"]
    tags = " ".join(f"#{t.replace(' ', '_').replace('-', '_')}" for t in _topics(row))
    lines = [
        f"<b>{esc(row['title'])}</b>",
        f"🎯 relevance <b>{row['relevance']}</b>/10 · ⭐ importance <b>{row['importance']}</b>/10",
        f"{esc(row['primary_cat'])} · {esc(row['created'])} · {esc(who)}",
    ]
    if row["tldr"]:
        lines.append(f"\n{esc(row['tldr'])}")
    if row["reason"]:
        lines.append(f"<i>{esc(row['reason'])}</i>")
    if tags:
        lines.append(esc(tags))
    links = [f'<a href="https://arxiv.org/abs/{html.escape(pid)}">abs</a>',
             f'<a href="https://arxiv.org/html/{html.escape(pid)}">html</a>',
             f'<a href="https://arxiv.org/pdf/{html.escape(pid)}">pdf</a>']
    if public_url:
        links.append(f'<a href="{html.escape(public_url)}/paper/{html.escape(pid)}">details</a>')
    lines.append("\n" + " · ".join(links))
    return "\n".join(lines)


def format_digest(day: str, n_new: int, n_analyzed: int, n_relevant: int, shown: Sequence[Mapping],
                  min_relevance: int, public_url: str) -> str:
    lines = [f"📚 <b>arXiv update {esc(day)}</b>",
             f"{n_new} new papers · {n_analyzed} analysed by Gemini",
             f"{n_relevant} match your interests (relevance ≥ {min_relevance})"]
    if n_relevant > len(shown):
        lines.append(f"Top {len(shown)} below, {n_relevant - len(shown)} more in the UI.")
    if public_url:
        lines.append(f'<a href="{html.escape(public_url)}/?days=7&amp;min_rel={min_relevance}&amp;sort=relevance">'
                     f"Open new relevant papers</a>")
    return "\n".join(lines)
