import httpx
import pytest
from fastapi.testclient import TestClient

from arxiv_search import affiliations as af
from arxiv_search import db
from arxiv_search.affiliations import (FetchError, HeaderFetcher, header_from_html, header_from_pdf_text,
                                       read_affiliations)
from arxiv_search.models import Org
from arxiv_search.orgparse import parse_block, parse_segment, registrable
from arxiv_search.pipeline import extract_affiliations
from arxiv_search.web import create_app
from tests.conftest import make_paper

# Shape of arXiv's LaTeXML output (arxiv.org/html/...), trimmed.
HTML = """<html><body><article class="ltx_document ltx_authors_1line">
<h1 class="ltx_title ltx_title_document">Nothing Left Behind</h1>
<div class="ltx_authors">
<span class="ltx_creator ltx_role_author">
<span class="ltx_personname"><span class="ltx_text ltx_font_bold">Giuliano Martinelli<sup class="ltx_sup"><span class="ltx_text">*</span></sup></span>,
<span class="ltx_text ltx_font_bold">Linxi Wang</span>
</span></span>
<span class="ltx_author_before">  </span><span class="ltx_creator ltx_role_author">
<span class="ltx_personname"><span class="ltx_text ltx_font_bold">Samuel Paul Fraiberger</span>Development Impact AI Lab, World Bank Group
</span></span>
<span class="ltx_author_before">  </span><span class="ltx_creator ltx_role_author">
<span class="ltx_personname"><sup class="ltx_sup">*</sup>Lead author. Correspondence: <a href="mailto:sf@worldbankgroup.org" class="ltx_ref">sf@worldbankgroup.org</a> &amp; co
</span></span></div>
<div id="abstract1" class="ltx_abstract"><h6>Abstract</h6><p>We search.</p></div>
</article></body></html>"""
AUTHORS = ["Giuliano Martinelli", "Linxi Wang", "Samuel Paul Fraiberger"]


def orgs(text, authors):
    return [(o.name, o.unit, o.kind, o.domain, o.authors) for o in parse_block(text, authors)]


def test_header_from_html():
    t = header_from_html(HTML)
    assert t.splitlines()[0] == "Giuliano Martinelli^*,"
    assert "Samuel Paul Fraiberger Development Impact AI Lab, World Bank Group" in t
    assert "Correspondence: sf@worldbankgroup.org & co" in t
    assert "Abstract" not in t and "<" not in t and "Nothing Left Behind" not in t
    assert header_from_html("<html><p>no authors block</p></html>") is None


def test_header_from_pdf_text():
    text = "A Title\nAda Lovelace1, Alan Turing2\n1Analytical Engines Ltd 2University of Manchester\nAbstract\nWe..."
    t = header_from_pdf_text(text)
    assert t.endswith("University of Manchester") and "We..." not in t
    assert header_from_pdf_text("   ") is None


# Author blocks below are shaped like real ones from the sample (arXiv HTML and PDF).

def test_one_affiliation_after_everyone():
    assert orgs(header_from_html(HTML), AUTHORS) == [
        ("World Bank Group", "Development Impact AI Lab", "government", "worldbankgroup.org", AUTHORS)]


def test_numbered_markers():
    text = ("Yifei Ge^1, Weisong Sun^2, Xia Feng^3, Yang Liu^2\n"
            "^1 Nanjing University ^2 Nanyang Technological University ^3 Hainan University\n"
            "gyf@126.com, {weisong.sun,yangliu}@ntu.edu.sg, xiafeng@hainanu.edu.cn")
    assert orgs(text, ["Yifei Ge", "Weisong Sun", "Xia Feng", "Yang Liu"]) == [
        ("Nanjing University", "", "academia", "", ["Yifei Ge"]),
        ("Nanyang Technological University", "", "academia", "ntu.edu.sg", ["Weisong Sun", "Yang Liu"]),
        ("Hainan University", "", "academia", "hainanu.edu.cn", ["Xia Feng"])]


def test_pdf_glued_markers_and_units():
    text = ("GEAR-Up: Generative AI and External Knowledge-based Retrieval\n"
            "Kaushik Roy*1, Harshul Surana 1, Heather\nHeckman2\n"
            "1Artificial Intelligence Institute, University of South Carolina\n"
            "2Library Sciences, University of South Carolina")
    assert orgs(text, ["Kaushik Roy", "Harshul Surana", "Heather Heckman"]) == [
        ("University of South Carolina", "Artificial Intelligence Institute; Library Sciences", "academia", "",
         ["Kaushik Roy", "Harshul Surana", "Heather Heckman"])]


def test_labeled_lines_per_author():
    text = ("Minchan Kim\nAffiliation: Graduate School of Data Science\nAffiliation: Seoul National University\n"
            "Affiliation: Seoul, Republic of Korea\nEmail: mk@snu.ac.kr\n"
            "Jungmin Hwang\nAffiliation: Department of Data Science\n"
            "Affiliation: Seoul National University of Science and Technology\nAffiliation: Seoul, Republic of Korea\n"
            "Aoyu Zhang^†\nAffiliation: Amazon Web Services\nBeijing, China\n"
            "Vishal Pandey\nAffiliation: London, UK\nEmail: vishal@metriqual.com")
    assert orgs(text, ["Minchan Kim", "Jungmin Hwang", "Aoyu Zhang", "Vishal Pandey"]) == [
        ("Seoul National University", "Graduate School of Data Science", "academia", "snu.ac.kr", ["Minchan Kim"]),
        ("Seoul National University of Science and Technology", "Department of Data Science", "academia", "",
         ["Jungmin Hwang"]),
        ("Amazon", "Amazon Web Services", "company", "", ["Aoyu Zhang"])]  # London is a city, not an org


def test_ieee_are_with_and_company_suffix():
    text = ("Long Zhao†\nBin He\n^† ^† thanks: This work was supported in part by grants 62573322. "
            "^† ^† thanks: The authors are with the Department of Control Science & Engineering, Tongji "
            "University, Shanghai 201804, China (e-mail: lz@tongji.edu.cn).")
    assert orgs(text, ["Long Zhao", "Bin He"]) == [
        ("Tongji University", "Department of Control Science & Engineering", "academia", "tongji.edu.cn",
         ["Long Zhao", "Bin He"])]
    assert [(o.name, o.kind) for o in parse_segment("Legal Technology & Data, Ankura Consulting Group, LLC, "
                                                    "Washington, D.C. USA")] == [
        ("Ankura Consulting Group, LLC", "company")]


def test_glued_names_and_known_orgs():
    text = "Itamar PresLaura RuisBelinda Z. Li^* Jacob Andreas^* MIT CSAIL"
    assert orgs(text, ["Itamar Pres", "Laura Ruis", "Belinda Z. Li", "Jacob Andreas"]) == [
        ("Massachusetts Institute of Technology", "MIT CSAIL", "academia", "",
         ["Itamar Pres", "Laura Ruis", "Belinda Z. Li", "Jacob Andreas"])]
    seg = lambda s: [(o.name, o.unit, o.kind) for o in parse_segment(s, labeled=True)]
    assert seg("Researcher, Global Product Strategy, F. Hoffmann-La Roche Ltd., Basel, Switzerland") == [
        ("F. Hoffmann-La Roche Ltd", "Global Product Strategy", "company")]
    assert seg("Aalto University & ELLIS Institute, Helsinki, Finland") == [
        ("Aalto University", "", "academia"), ("ELLIS Institute", "", "other")]
    assert seg("Korea Advanced Institute of Science and Technology") == [
        ("Korea Advanced Institute of Science and Technology", "", "academia")]
    assert seg("Services AI Research Group, Dell Technologies") == [
        ("Dell Technologies", "Services AI Research Group", "company")]


def test_similar_names_do_not_steal():
    text = "Di Zhao\nAffiliation: National University of Defense Technology\nZhao Lv\nAffiliation: Academy of Military Sciences"
    assert [(n, a) for n, _, _, _, a in orgs(text, ["Di Zhao", "Zhao Lv"])] == [
        ("National University of Defense Technology", ["Di Zhao"]), ("Academy of Military Sciences", ["Zhao Lv"])]


def test_registrable_domain():
    assert registrable("mail.ustc.edu.cn") == "ustc.edu.cn"
    assert registrable("cs.stanford.edu") == "stanford.edu"
    assert registrable("connect.polyu.hk") == "polyu.hk"


class FakeFetcher:
    def __init__(self, html=None, pdf=None):
        self.html_blocks, self.pdf_blocks, self.calls = html or {}, pdf or {}, []

    def html(self, pid):
        self.calls.append(("html", pid))
        b = self.html_blocks.get(pid)
        if isinstance(b, Exception):
            raise b
        return b

    def pdf(self, pid):
        self.calls.append(("pdf", pid))
        return self.pdf_blocks.get(pid)


def test_pdf_fallback_when_html_has_no_affiliations():
    f = FakeFetcher(html={"1": "Kaushik Roy\nHeather Heckman"},
                    pdf={"1": "Kaushik Roy1, Heather Heckman1\n1University of South Carolina"})
    source, header, found = read_affiliations(f, "1", ["Kaushik Roy", "Heather Heckman"])
    assert source == "pdf" and found[0].name == "University of South Carolina"
    f = FakeFetcher(html={"2": header_from_html(HTML)})
    assert read_affiliations(f, "2", AUTHORS)[0] == "html" and f.calls == [("html", "2")]  # no PDF download
    assert read_affiliations(FakeFetcher(), "3", []) is None


def test_fetcher_urls(monkeypatch, no_sleep):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if "/html/2610.00001" in str(req.url):
            return httpx.Response(200, text=HTML)
        if "/pdf/2610.00002" in str(req.url):
            return httpx.Response(200, content=b"%PDF-1.7 fake")
        return httpx.Response(404)

    monkeypatch.setattr(af, "header_from_pdf", lambda data: "Ada Lovelace\nAnalytical Engines Ltd")
    f = HeaderFetcher(client=httpx.Client(transport=httpx.MockTransport(handler)), min_gap=0)
    assert "World Bank Group" in f.html("2610.00001")
    assert f.html("2610.00002") is None and f.pdf("2610.00002") == "Ada Lovelace\nAnalytical Engines Ltd"
    assert f.pdf("2610.00003") is None
    assert calls == ["https://arxiv.org/html/2610.00001", "https://arxiv.org/html/2610.00002",
                     "https://arxiv.org/pdf/2610.00002", "https://arxiv.org/pdf/2610.00003"]


def test_fetcher_raises_when_arxiv_down(no_sleep):
    f = HeaderFetcher(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
                      min_gap=0, max_retries=2)
    with pytest.raises(FetchError):
        f.html("2610.00001")


def test_extract_store_and_search(settings, con, no_sleep):
    db.upsert_papers(con, [make_paper("2610.36910", title="Nothing Left Behind", authors=AUTHORS),
                           make_paper("2610.00002", title="Rankers at scale", authors=["Ada Lovelace"]),
                           make_paper("2610.00003", title="No source paper"),
                           make_paper("2610.00004", title="Network blip")], added_at=db.now_iso())
    f = FakeFetcher(html={"2610.36910": header_from_html(HTML),
                          "2610.00002": "Ada Lovelace\nAffiliation: Google DeepMind, London, UK",
                          "2610.00004": FetchError("arXiv unreachable")})
    assert extract_affiliations(con, f, db.pending_affiliations(con, only_added=True)) == 3
    st = {r["paper_id"]: (r["status"], r["model"]) for r in con.execute("SELECT * FROM affil_status")}
    assert st == {"2610.36910": ("ok", "rules-1"), "2610.00002": ("ok", "rules-1"),
                  "2610.00003": ("no_source", "rules-1")}
    assert [p.id for p in db.pending_affiliations(con)] == ["2610.00004"]  # retried next time

    c = TestClient(create_app(settings))
    for q in ("World Bank", "worldbankgroup.org", "development impact"):
        r = c.get("/", params={"org": q})
        assert "Nothing Left Behind" in r.text and "Rankers at scale" not in r.text, q
    r = c.get("/", params={"kind": "company"})
    assert "Rankers at scale" in r.text and "Nothing Left Behind" not in r.text
    r = c.get("/", params={"author": "Samuel Fraiberger"})
    assert "Nothing Left Behind" in r.text and "Rankers at scale" not in r.text
    r = c.get("/", params={"org": "World Bank"})
    assert 'href="/?org=World%20Bank%20Group"' in r.text and 'href="/?author=Samuel%20Paul%20Fraiberger"' in r.text
    r = c.get("/paper/2610.36910")
    assert "Development Impact AI Lab" in r.text and "worldbankgroup.org" in r.text and "rules-1" in r.text
    assert "No HTML or PDF" in c.get("/paper/2610.00003").text
    assert "Google DeepMind" in c.get("/stats").text


def test_save_replaces_and_reparse_uses_stored_header(con):
    db.upsert_papers(con, [make_paper("2610.00001", authors=["Ada Lovelace"])], added_at=None)
    db.save_affiliations(con, "2610.00001", [Org("Old Corp"), Org("Other")], source="html",
                         header="Ada Lovelace\nAffiliation: University of Manchester", parser="rules-0")
    assert con.execute("SELECT count(*) FROM orgs_fts WHERE orgs_fts MATCH 'Old'").fetchone()[0] == 1
    for r in db.stored_headers(con):  # what `arxiv-search affiliations --reparse` does
        db.save_affiliations(con, r["paper_id"], parse_block(r["header"], r["authors"].split("; ")),
                             source=r["source"], header=r["header"], parser="rules-1")
    assert [r["name"] for r in db.orgs_for(con, ["2610.00001"])["2610.00001"]] == ["University of Manchester"]
    assert con.execute("SELECT count(*) FROM orgs_fts WHERE orgs_fts MATCH 'Old'").fetchone()[0] == 0


def test_author_match_variants():
    from arxiv_search.db import author_match
    authors = "Ada Lovelace; Samuel P. Fraiberger; José Ángel García"
    for q in ("Samuel Fraiberger", "Samuel P. Fraiberger", "S Fraiberger", "S. P. Fraiberger", "fraiberger",
              "Jose Garcia", "José Ángel García"):
        assert author_match(authors, q), q
    for q in ("Fraiberger Samuel", "Samuel Lovelace", "Lovelace Samuel", "Sam Fraiberger", "Ada Fraiberger", ""):
        assert not author_match(authors, q), q


def test_shared_rate_limit_spaces_requests_across_instances(tmp_path):
    import time
    from arxiv_search.ratelimit import RateLimit
    a, b = RateLimit(0.3, tmp_path / "rl"), RateLimit(0.3, tmp_path / "rl")  # e.g. update + backfill
    t0 = time.time()
    a.wait(); b.wait(); a.wait()
    assert time.time() - t0 >= 0.55
