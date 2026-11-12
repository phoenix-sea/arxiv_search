"""Gemini: is each new paper worth interest? (yes/no)"""
from __future__ import annotations

import json
import logging
import time
from typing import Sequence

import httpx

from .models import Paper

log = logging.getLogger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BATCH = 20  # papers per request
MIN_GAP = 6.0  # seconds between requests

# Placeholder until the prompt is written from the labels.
PROMPT = """The reader follows information retrieval, ranking, relevance, LLMs and generative retrieval.
For each paper, answer whether it is worth the reader's interest.
Return one object per paper, copying its id unchanged."""

SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT",
                                     "properties": {"id": {"type": "STRING"}, "interesting": {"type": "BOOLEAN"}},
                                     "required": ["id", "interesting"]}}


class GeminiError(RuntimeError):
    pass


class Gemini:
    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None):
        self.api_key, self.model = api_key, model
        self.http = client or httpx.Client(timeout=180)
        self._last = 0.0

    def flag(self, papers: Sequence[Paper]) -> dict[str, bool]:
        """{paper id: interesting} for the papers Gemini answered."""
        out: dict[str, bool] = {}
        for i in range(0, len(papers), BATCH):
            batch = papers[i:i + BATCH]
            text = "\n".join(f'<paper id="{p.id}">\nTitle: {p.title}\nAbstract: {p.abstract}\n</paper>' for p in batch)
            try:
                items = json.loads(self._call(text))
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                log.warning("unusable Gemini answer (%s); %d papers retried next run", e, len(batch))
                continue
            ids = {p.id for p in batch}
            out.update({x["id"]: bool(x["interesting"]) for x in items
                        if isinstance(x, dict) and x.get("id") in ids})
        return out

    def _call(self, text: str) -> str:
        body = {"systemInstruction": {"parts": [{"text": PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA}}
        for attempt in range(1, 6):
            wait = MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.http.post(ENDPOINT.format(model=self.model), json=body,
                                   headers={"x-goog-api-key": self.api_key})
            except httpx.HTTPError as e:
                log.warning("Gemini %s, retry %d/5", type(e).__name__, attempt)
                time.sleep(10 * attempt)
                continue
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            if r.status_code in (429, 500, 502, 503, 504):
                log.warning("Gemini HTTP %s, retry %d/5", r.status_code, attempt)
                time.sleep(30 * attempt)
                continue
            raise GeminiError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
        raise GeminiError("Gemini failed after 5 attempts")
