"""Gemini relevance/importance triage via the REST generateContent endpoint.

Papers are scored in batches (one request per GEMINI_BATCH_SIZE papers) with
structured JSON output. Robustness:
  * 429/5xx -> backoff and retry (honours Retry-After / RetryInfo)
  * truncated or unparsable output -> split the batch in half and retry
  * papers the model skipped -> retried in a follow-up request
  * a model that rejects `thinkingConfig` -> retried once without it
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Sequence

import httpx

from .models import Assessment, Paper

log = logging.getLogger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# USD per 1M tokens, standard tier (ai.google.dev pricing page, checked 2026-11-11).
PRICES = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
# Rough per-paper token use for cost estimates (abstract ~300 tokens + share of prompt; short JSON out).
EST_IN, EST_OUT = 420, 140

SYSTEM_PROMPT = """You triage new arXiv papers for one researcher. Judge each paper from its title, \
categories and abstract only. Score two independent integer scales from 0 to 10.

relevance: how closely the paper matches the researcher's interest profile.
  0-2 unrelated · 3-4 tangential · 5-6 adjacent area · 7-8 directly relevant · 9-10 core, must-read.
importance: likely significance to its field, independent of the profile: novelty, strength of \
evidence, scale (e.g. large-scale or production deployment, strong benchmarks), likely influence, \
useful released resources (datasets, code, benchmarks, models).
  0-2 weak or minor · 3-4 incremental · 5-6 solid · 7-8 notable · 9-10 likely landmark.
Be calibrated and sceptical: most papers score 3-6 on importance; do not reward hype words.

Also return:
  topics: 1-4 short lowercase topic tags
  tldr: one sentence, at most 25 words, what the paper does and finds
  reason: at most 20 words, why it is or is not relevant to the profile

Return exactly one object per input paper, copying its id unchanged."""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "relevance": {"type": "INTEGER"},
            "importance": {"type": "INTEGER"},
            "topics": {"type": "ARRAY", "items": {"type": "STRING"}},
            "tldr": {"type": "STRING"},
            "reason": {"type": "STRING"},
        },
        "required": ["id", "relevance", "importance", "topics", "tldr", "reason"],
        "propertyOrdering": ["id", "relevance", "importance", "topics", "tldr", "reason"],
    },
}


class GeminiError(RuntimeError):
    pass


class _Retryable(Exception):
    """Output truncated, unparsable or blocked: split the batch and retry."""


@dataclass
class AnalysisResult:
    assessments: list[Assessment] = field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def estimate_cost(model: str, n_papers: int) -> float | None:
    if model not in PRICES:
        return None
    pin, pout = PRICES[model]
    return n_papers * (EST_IN * pin + EST_OUT * pout) / 1e6


def build_user_prompt(profile: str, papers: Sequence[Paper]) -> str:
    parts = [f"Researcher interest profile:\n<profile>\n{profile}\n</profile>\n\nPapers:"]
    for p in papers:
        parts.append(f'<paper id="{p.id}">\nTitle: {p.title}\nCategories: {" ".join(p.categories)}\n'
                     f"Abstract: {p.abstract}\n</paper>")
    return "\n".join(parts)


def _clamp(v) -> int:
    try:
        return max(0, min(10, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


def parse_assessments(data, wanted: Sequence[str]) -> dict[str, Assessment]:
    want = set(wanted)
    out: dict[str, Assessment] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        pid = re.sub(r"v\d+$", "", str(item.get("id", "")).strip())
        if pid not in want or pid in out:
            continue
        topics = item.get("topics") or []
        out[pid] = Assessment(
            id=pid, relevance=_clamp(item.get("relevance")), importance=_clamp(item.get("importance")),
            topics=[str(t).strip().lower()[:40] for t in topics if str(t).strip()][:4] if isinstance(topics, list) else [],
            tldr=str(item.get("tldr") or "").strip()[:400],
            reason=str(item.get("reason") or "").strip()[:300])
    return out


def _retry_delay(r: httpx.Response, attempt: int) -> float:
    ra = r.headers.get("Retry-After", "")
    if ra.isdigit():
        return float(ra)
    try:  # google.rpc.RetryInfo, e.g. "retryDelay": "17s"
        for d in r.json().get("error", {}).get("details", []):
            if "retryDelay" in d:
                return float(str(d["retryDelay"]).rstrip("s")) + 1
    except Exception:
        pass
    return min(5 * 2 ** attempt, 300)


class GeminiAnalyzer:
    def __init__(self, api_key: str, model: str, profile: str, *, thinking_level: str = "minimal",
                 rpm: int = 10, client: httpx.Client | None = None, max_retries: int = 6,
                 max_output_tokens: int = 8192):
        if not api_key:
            raise GeminiError("GEMINI_API_KEY is not set")
        self.api_key, self.model, self.profile = api_key, model, profile
        self.thinking_level = thinking_level
        self.min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self.http = client or httpx.Client(timeout=180)
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self._last = 0.0

    # -- public -------------------------------------------------------------
    def analyze(self, papers: Sequence[Paper]) -> AnalysisResult:
        res = AnalysisResult()
        self._analyze_into(list(papers), res, depth=0)
        return res

    # -- internals ----------------------------------------------------------
    def _analyze_into(self, papers: list[Paper], res: AnalysisResult, depth: int) -> None:
        if not papers:
            return
        try:
            data = self._call(papers, res)
        except _Retryable as e:
            if len(papers) > 1:
                log.info("splitting batch of %d (%s)", len(papers), e)
                mid = len(papers) // 2
                self._analyze_into(papers[:mid], res, depth)
                self._analyze_into(papers[mid:], res, depth)
            else:
                log.warning("no usable Gemini result for %s (%s); storing placeholder", papers[0].id, e)
                res.assessments.append(Assessment(id=papers[0].id, relevance=0, importance=0,
                                                  reason=f"not analysed: {e}"[:300]))
            return
        found = parse_assessments(data, [p.id for p in papers])
        res.assessments.extend(found[p.id] for p in papers if p.id in found)
        missing = [p for p in papers if p.id not in found]
        if missing:
            if depth < 2:
                log.info("model skipped %d papers; retrying them", len(missing))
                self._analyze_into(missing, res, depth + 1)
            else:
                log.warning("giving up on %d papers the model keeps skipping", len(missing))

    def _body(self, papers: Sequence[Paper]) -> dict:
        cfg: dict = {"responseMimeType": "application/json", "responseSchema": RESPONSE_SCHEMA,
                     "maxOutputTokens": self.max_output_tokens}
        if self.thinking_level:
            cfg["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        return {"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": build_user_prompt(self.profile, papers)}]}],
                "generationConfig": cfg}

    def _call(self, papers: Sequence[Paper], res: AnalysisResult):
        url = ENDPOINT.format(model=self.model)
        for attempt in range(1, self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.http.post(url, json=self._body(papers), headers={"x-goog-api-key": self.api_key})
            except httpx.HTTPError as e:
                delay = min(5 * 2 ** attempt, 300)
                log.warning("Gemini %s, retry %d/%d in %ds", type(e).__name__, attempt, self.max_retries, delay)
                time.sleep(delay)
                continue
            if r.status_code == 200:
                return self._parse_response(r.json(), res)
            msg = self._error_message(r)
            if r.status_code == 400 and self.thinking_level and "think" in msg.lower():
                log.warning("model %s rejected thinkingConfig (%s); disabling it", self.model, msg)
                self.thinking_level = ""
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                delay = _retry_delay(r, attempt)
                log.warning("Gemini HTTP %s (%s), retry %d/%d in %.0fs", r.status_code, msg[:120], attempt,
                            self.max_retries, delay)
                time.sleep(delay)
                continue
            raise GeminiError(f"Gemini HTTP {r.status_code}: {msg}")
        raise GeminiError(f"Gemini failed after {self.max_retries} attempts")

    @staticmethod
    def _error_message(r: httpx.Response) -> str:
        try:
            return r.json().get("error", {}).get("message", "") or r.text[:300]
        except ValueError:
            return r.text[:300]

    def _parse_response(self, j: dict, res: AnalysisResult):
        res.calls += 1
        um = j.get("usageMetadata") or {}
        res.input_tokens += int(um.get("promptTokenCount") or 0)
        res.output_tokens += int(um.get("candidatesTokenCount") or 0) + int(um.get("thoughtsTokenCount") or 0)
        cands = j.get("candidates") or []
        if not cands:
            raise _Retryable(f"blocked: {(j.get('promptFeedback') or {}).get('blockReason', 'no candidates')}")
        c = cands[0]
        finish = c.get("finishReason", "")
        text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []) if not p.get("thought"))
        if finish == "MAX_TOKENS":
            raise _Retryable("output truncated")
        if finish not in ("", "STOP", "FINISH_REASON_UNSPECIFIED") and not text:
            raise _Retryable(f"finishReason {finish}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise _Retryable("unparsable JSON")
