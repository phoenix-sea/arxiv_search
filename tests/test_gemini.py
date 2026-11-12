import json

import httpx
import pytest

from arxiv_search.gemini import GeminiAnalyzer, GeminiError, estimate_cost, parse_assessments
from tests.conftest import make_paper


def ok(items, finish="STOP", thought=False):
    parts = [{"text": "thinking...", "thought": True}] if thought else []
    parts.append({"text": json.dumps(items)})
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": parts}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 5}})


def item(pid, rel=8, imp=6):
    return {"id": pid, "relevance": rel, "importance": imp, "topics": ["Ranking", "LLM"], "tldr": "t", "reason": "r"}


def analyzer(handler, **kw):
    return GeminiAnalyzer("key", "gemini-3.5-flash-lite", "profile", rpm=0,
                          client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def ids_in(req):
    text = json.loads(req.content)["contents"][0]["parts"][0]["text"]
    return [line.split('"')[1] for line in text.splitlines() if line.startswith("<paper id=")]


def test_request_shape_and_parse(no_sleep):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["key"] = req.headers["x-goog-api-key"]
        seen["url"] = str(req.url)
        return ok([item(i) for i in ids_in(req)], thought=True)

    res = analyzer(handler).analyze([make_paper("2611.00001"), make_paper("2611.00002")])
    assert [a.id for a in res.assessments] == ["2611.00001", "2611.00002"]
    assert res.assessments[0].topics == ["ranking", "llm"]
    assert (res.calls, res.input_tokens, res.output_tokens) == (1, 100, 25)
    cfg = seen["body"]["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert "temperature" not in cfg
    assert seen["key"] == "key" and "key" not in seen["url"]
    assert seen["url"].endswith("/models/gemini-3.5-flash-lite:generateContent")


def test_thinking_rejected_falls_back(no_sleep):
    bodies = []

    def handler(req):
        b = json.loads(req.content)
        bodies.append(b)
        if "thinkingConfig" in b["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Thinking level is not supported for this model."}})
        return ok([item(i) for i in ids_in(req)])

    an = analyzer(handler)
    assert len(an.analyze([make_paper()]).assessments) == 1
    assert len(bodies) == 2 and an.thinking_level == ""


def test_truncation_splits_batch(no_sleep):
    def handler(req):
        ids = ids_in(req)
        if len(ids) > 1:
            return ok([], finish="MAX_TOKENS")
        return ok([item(ids[0])])

    res = analyzer(handler).analyze([make_paper(f"2611.0000{i}") for i in range(4)])
    assert sorted(a.id for a in res.assessments) == [f"2611.0000{i}" for i in range(4)]


def test_skipped_papers_are_retried(no_sleep):
    def handler(req):
        ids = ids_in(req)
        return ok([item(ids[0] + "v1")])  # model answers only the first, with a version suffix

    res = analyzer(handler).analyze([make_paper("2611.00001"), make_paper("2611.00002")])
    assert sorted(a.id for a in res.assessments) == ["2611.00001", "2611.00002"]


def test_429_then_success(no_sleep):
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, json={"error": {"message": "quota", "details": [{"retryDelay": "3s"}]}})
        return ok([item(i) for i in ids_in(req)])

    assert len(analyzer(handler).analyze([make_paper()]).assessments) == 1


def test_auth_error_raises(no_sleep):
    with pytest.raises(GeminiError):
        analyzer(lambda r: httpx.Response(403, json={"error": {"message": "API key not valid"}})).analyze([make_paper()])


def test_parse_clamps_and_ignores_unknown():
    out = parse_assessments([{"id": "x", "relevance": 14, "importance": "-3", "topics": "bad"},
                             {"id": "zzz", "relevance": 5}, "junk"], ["x"])
    assert (out["x"].relevance, out["x"].importance, out["x"].topics) == (10, 0, [])
    assert list(out) == ["x"]


def test_estimate_cost():
    assert estimate_cost("gemini-3.5-flash-lite", 1000) == pytest.approx(0.476)
    assert estimate_cost("unknown-model", 10) is None
