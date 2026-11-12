import datetime as dt
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from arxiv_search.arxiv_api import ArxivApiError, ArxivClient, build_query, parse_feed

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>{total}</opensearch:totalResults>
  {entries}
</feed>"""

ENTRY = """<entry>
    <id>http://arxiv.org/abs/{pid}v2</id>
    <updated>2026-11-10T12:00:00Z</updated>
    <published>2026-11-09T08:00:00Z</published>
    <title>Semantic Search
      At Scale</title>
    <summary>  We rank
      things.  </summary>
    <author><name>Fedor Borisyuk</name></author>
    <author><name>Jane Doe</name></author>
    <arxiv:doi>10.1/x</arxiv:doi>
    <arxiv:comment>12 pages</arxiv:comment>
    <arxiv:primary_category term="cs.IR"/>
    <category term="cs.AI"/><category term="cs.IR"/><category term="cs.LG"/>
  </entry>"""

ERROR = FEED.format(total=1, entries="""<entry><id>https://arxiv.org/api/errors</id><title>Error</title>
  <summary>The server encountered an internal error</summary></entry>""")


def test_parse_feed():
    total, papers = parse_feed(FEED.format(total=1, entries=ENTRY.format(pid="2611.01234")))
    p = papers[0]
    assert total == 1
    assert p.id == "2611.01234"
    assert p.title == "Semantic Search At Scale"
    assert p.abstract == "We rank things."
    assert p.categories == ["cs.IR", "cs.AI", "cs.LG"]  # primary first
    assert (p.created, p.updated) == ("2026-11-09", "2026-11-10")
    assert p.authors == ["Fedor Borisyuk", "Jane Doe"]
    assert p.doi == "10.1/x" and p.comments == "12 pages"


def test_old_style_id():
    _, papers = parse_feed(FEED.format(total=1, entries=ENTRY.format(pid="cs/0101001")))
    assert papers[0].id == "cs/0101001"


def test_error_feed_raises():
    with pytest.raises(ArxivApiError):
        parse_feed(ERROR)


def test_query():
    q = build_query(["cs.IR", "cs.CL"], dt.date(2026, 11, 1), dt.date(2026, 11, 10))
    assert q == "(cat:cs.IR OR cat:cs.CL) AND submittedDate:[202611010000 TO 202611102359]"


def test_fetch_pages_and_retries(no_sleep):
    calls = []

    def handler(req):
        start = int(parse_qs(urlparse(str(req.url)).query)["start"][0])
        calls.append(start)
        if len(calls) == 2:
            return httpx.Response(503)
        if len(calls) == 3:
            return httpx.Response(200, text=ERROR)  # transient API error page
        ids = {0: ["2611.00001", "2611.00002"], 2: ["2611.00003"]}[start]
        return httpx.Response(200, text=FEED.format(total=3, entries="".join(ENTRY.format(pid=i) for i in ids)))

    c = ArxivClient(client=httpx.Client(transport=httpx.MockTransport(handler)), min_gap=0)
    got = [p.id for p in c.fetch_submitted(["cs.IR"], dt.date(2026, 11, 1), dt.date(2026, 11, 2), page_size=2)]
    assert got == ["2611.00001", "2611.00002", "2611.00003"]
    assert calls == [0, 2, 2, 2]
