"""Regex extraction of affiliations from a paper's author block.

Input: the author block as text lines (arXiv HTML, or page 1 of the PDF) and the
arXiv metadata author list (used to cut the names out). Output: organizations
with unit and email domain.

  1. email domains are collected; the title (lines before the first author name),
     notes (correspondence, equal contribution, funding, ...) and everything from
     "Abstract"/"Keywords"/"Figure 1" on are dropped;
  2. author names are cut out of each line;
  3. what is left is grouped into affiliations: a new one starts at a marker
     ("^1 University ..."), after ";", after an author name, or when a line starts
     another organization;
  4. in each affiliation's comma parts the organization is the part with an
     organization keyword (University, Inc., Ministry, ...), else the strongest
     organization-like part (institute, lab, ... group), else the longest part;
     the parts before it are its unit; addresses are dropped.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Sequence

from .models import Org

SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
SYMBOLS = "*†‡§¶#♯∗⋆✝♮♭♢♠♣♡"
MARK_RE = rf"(?:\^?(?:\d{{1,2}}|[{SYMBOLS}]))"
LOWER = "a-zß-öø-ÿ"

TOP_RE = re.compile(  # words that make a part a top-level organization
    r"universit|\bcollege\b(?!\s+of\b)|institute of (?:science and )?technology|polytechnic|politecnico|"
    r"polytechnique|école|\becole\b|hochschule|medical school|school of (?:medicine|public health)\b|"
    r"institut\w* (?:of|for|de|für) |academy of sciences|"
    r"\S.*\b(?:inc|ltd|llc|corp|corporation|gmbh|plc|pvt)\b\.?|\btechnologies\b|\btechnology$|\bconsulting\b|"
    r"\b(?:ministry|government|agency|bureau|national (?:laborator\w*|lab))\b|\bfoundation\b", re.I)
ORG_RE = re.compile(r"\b(institut\w*|center|centre|laborator\w*|labs?|research|academy|hospital|clinic|council)\b",
                    re.I)
UNIT_PREFIX_RE = re.compile(  # parts inside an organization
    r"^(?:the )?(dep(?:artment|t)\b\.?|dept\b|school of|faculty|division|chair\b|college of|graduate school|"
    r"key lab|state key|national key|lab(?:oratory)? (?:of|for)|program(?:me)? (?:in|of))", re.I)
UNIT_SUFFIX_RE = re.compile(r" (?:lab|laboratory|group|team|unit|program|programme)$", re.I)
GENERIC_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "qq.com",
                "163.com", "126.com", "foxmail.com", "icloud.com", "me.com", "protonmail.com", "proton.me",
                "mail.com", "aol.com", "yandex.ru", "sina.com", "gmx.de", "web.de"}
SECOND_LEVEL = {"ac", "edu", "co", "com", "gov", "org", "net", "or", "ne", "go", "res", "nic", "gob", "govt"}
COUNTRIES = {
    "usa", "us", "u.s", "u.s.a", "united states", "united states of america", "uk", "u.k", "united kingdom",
    "england", "scotland", "wales", "china", "p.r. china", "pr china", "p. r. china", "prc", "japan", "korea",
    "south korea", "republic of korea", "germany", "france", "italy", "spain", "portugal", "netherlands",
    "the netherlands", "belgium", "switzerland", "austria", "sweden", "norway", "denmark", "finland", "canada",
    "australia", "new zealand", "india", "singapore", "israel", "brazil", "mexico", "hong kong", "hong kong sar",
    "taiwan", "russia", "poland", "czech republic", "czechia", "greece", "turkey", "türkiye", "iran", "saudi arabia",
    "uae", "united arab emirates", "qatar", "egypt", "south africa", "nigeria", "kenya", "ireland", "luxembourg",
    "hungary", "romania", "vietnam", "viet nam", "thailand", "malaysia", "indonesia", "pakistan", "bangladesh",
    "chile", "argentina", "colombia", "macau", "macao", "estonia", "slovenia", "croatia", "serbia", "ukraine",
    "morocco", "tunisia", "jordan", "lebanon", "philippines", "nepal", "sri lanka", "peru", "uruguay", "cyprus",
}
NOTE_RE = re.compile(
    r"(correspond|equal(?:ly)? contribut|contributed equally|lead author|work (?:was )?done|work performed|"
    r"internship|interning|intern at|project lead|thanks:|preprint|under review|accepted|proceedings|"
    r"conference|workshop|copyright|licen[cs]e|arxiv|https?://|www\.|github|code (?:is )?available|"
    r"supported (?:in part )?by|funded by|grant|page \d|^\W*\d{1,2}\s*$|^—|senior author|co-first|"
    r"first author|these authors|denotes|indicates|^\W*(?:january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b|^\W*(?:19|20)\d\d\W*$)", re.I)
WITH_RE = re.compile(r"\b(?:is|are) (?:also |now )?(?:with|affiliated with) (?:the )?(.+?)(?:\(e-?mail|\.\s|$)",
                     re.I)
END_RE = re.compile(
    r"^\W*(keywords?|index terms|ccs concepts|acm reference|abstract|introduction|figure \d|fig\. ?\d|table \d|©)",
    re.I)
LABEL_RE = re.compile(r"^\s*(affiliations?|address|institutions?)\s*:\s*", re.I)
SKIP_LABEL_RE = re.compile(r"^\s*(e-?mails?|note|orcid|homepage|web|url|phone|tel|voice|fax|contact)\b\s*:?", re.I)
EMAIL_RE = re.compile(r"(?:\{[^}]{1,300}\}|[\w.+\-]+)\s?@\s?([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)", re.I)
STOP = {"of", "the", "and", "for", "de", "la", "di", "der", "für", "at", "in", "&"}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _clean(s: str) -> str:
    s = re.sub(rf"^[\s,;:.&\-–—()\[\]]*(?:{MARK_RE}[\s,]*)*", "", s)
    s = re.sub(rf"[\s,;:(\[]*(?:{MARK_RE}[\s,]*)*$", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;:.[]-–—")
    if s.count("(") < s.count(")"):
        s = s.lstrip(")")
    if s.count("(") > s.count(")"):
        s = s.rstrip("( ") + (")" if "(" in s.rstrip("( ") else "")
    return s


def registrable(domain: str) -> str:
    parts = domain.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_address(part: str) -> bool:
    p = _fold(part).strip(" .")
    return (p in COUNTRIES or bool(re.search(r"\d{3,}", p)) or bool(re.fullmatch(r"[a-z]{2}", p))
            or bool(re.search(r"\b(street|st\.|road|rd\.|avenue|ave\.|blvd|floor|room|building|campus|p\.?o\.? box|"
                              r"district|province|zip)\b", p)))


def _score(part: str) -> int:
    """3 organization · 2 organization-like (institute, center, lab) · 1 unit · 0 nothing."""
    if UNIT_PREFIX_RE.match(part):
        return 1
    if TOP_RE.search(part):
        return 3
    if UNIT_SUFFIX_RE.search(part):
        return 1
    return 2 if ORG_RE.search(part) else 0


def _parts(seg: str) -> tuple[list[str], bool]:
    """Comma parts without addresses; and whether an address was seen."""
    out, address = [], False
    for p in re.split(r",|\s+-\s+|\s+–\s+|\|", LABEL_RE.sub("", seg)):
        p = _clean(p)
        if not p or not re.search(r"[^\W\d_]{2}", p):
            continue
        if _is_address(p):
            address = True
        else:
            out.append(p)
    return out, address


def parse_segment(seg: str, labeled: bool = False) -> list[Org]:
    """Organizations in one affiliation, e.g. "Development Impact AI Lab, World Bank Group"."""
    parts, address = _parts(seg)
    if not parts:
        return []
    scores = [_score(p) for p in parts]
    top = max(scores)
    if top == 3:
        anchors = [i for i, sc in enumerate(scores) if sc == 3]
    elif top > 0:  # the last of the strongest: units come before their organization
        anchors = [max(i for i, sc in enumerate(scores) if sc == top)]
    elif labeled and not (address and len(parts) == 1 and len(parts[0].split()) <= 3):
        # no keyword: the longest part ("Amazon Web Services, Beijing"); a lone short name next to a
        # country is a city ("Affiliation: London, UK")
        anchors = [max(range(len(parts)), key=lambda i: (len(parts[i]), -i))]
    else:
        return []
    out, prev = [], -1
    for a in anchors:
        unit = ", ".join(parts[prev + 1:a][-2:])
        prev = a
        if len(parts[a]) <= 120 and len(parts[a].split()) <= 14:
            out.append(Org(name=parts[a], unit=unit[:160]))
    return out


def _name_patterns(name: str) -> list[re.Pattern]:
    """Full name (case-sensitive, tolerating glued text like "FraibergerDevelopment"), full name in any case,
    surname alone. Middle names and initials are optional."""
    toks = re.findall(r"[^\W\d_]+", name)
    if not toks:
        return []
    first, sur = re.escape(toks[0]), re.escape(toks[-1])
    mid = r"(?:[\s.\-]+[^\W\d_][\w.\-]*?){0,3}?[\s.\-]*"
    end = rf"(?![{LOWER}])"
    pats = []
    if len(toks) > 1:
        pats.append(re.compile(rf"(?<![A-Z]){first}{end}{mid}{sur}{end}"))
        pats.append(re.compile(rf"(?<![^\W\d_]){first}{end}{mid}{sur}{end}", re.I))
    pats.append(re.compile(rf"(?<![^\W\d_]){sur}{end}", re.I))
    return pats


def parse_block(text: str, authors: Sequence[str]) -> list[Org]:
    text = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+", lambda m: "^" + m.group().translate(SUPERSCRIPTS) + " ", text)
    domains = [d for d in dict.fromkeys(m.group(1).lower().rstrip(".") for m in EMAIL_RE.finditer(text))
               if registrable(d) not in GENERIC_MAIL]
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    pats = [p for p in (next((c for c in _name_patterns(a) if any(c.search(l) for l in lines)), None)
                        for a in authors) if p]
    first = next((i for i, l in enumerate(lines) if any(p.search(l) for p in pats)), 0)

    orgs: list[Org] = []
    cur: dict | None = None  # affiliation being collected over consecutive lines

    def close() -> None:
        nonlocal cur
        if cur:
            for org in parse_segment(" , ".join(cur["texts"]), cur["labeled"]):
                same = next((o for o in orgs if _fold(o.name) == _fold(org.name)), None)
                if not same:
                    orgs.append(org)
                elif org.unit and _fold(org.unit) not in _fold(same.unit):
                    same.unit = "; ".join(x for x in (same.unit, org.unit) if x)[:160]
        cur = None

    for line in lines[first:]:  # the title comes before the first author
        if END_RE.search(line):
            break
        with_ = WITH_RE.search(line)  # IEEE style: "The authors are with the Department of ..., X University"
        if with_:
            line = "Affiliation: " + re.sub(r",? and (?:also )?with (?:the )?", " ; ", with_.group(1))
        elif SKIP_LABEL_RE.match(line):
            continue
        line = EMAIL_RE.sub(" ", line)
        if not with_ and NOTE_RE.search(line):
            continue
        labeled = bool(LABEL_RE.match(line)) or bool(with_)
        line = LABEL_RE.sub("", line)
        if labeled and cur is not None and max(map(_score, cur["parts"]), default=0) not in (1, 2):
            close()  # "Affiliation: X" lines are separate affiliations unless the previous one was only a unit

        spans = sorted((m.start(), m.end()) for p in pats for m in p.finditer(line))
        rest, pos = [], 0
        for st, en in spans:
            if st >= pos:
                rest.append(line[pos:st])
                pos = en
        rest.append(line[pos:])
        if spans:
            close()

        pieces = re.split(rf"(?:(?<=\s)|^|(?<=,))(?=\^?(?:\d{{1,2}}|[{SYMBOLS}])\s*[A-Z])|;", " , ".join(rest))
        for k, piece in enumerate(pieces):
            if not re.search(r"[^\W\d_]{2}", piece) or _clean(piece).lower() in STOP | {"with", "et al"}:
                continue
            parts = _parts(piece)[0]
            if cur is not None:
                cur_top = max(map(_score, cur["parts"]), default=0)
                new_top = max(map(_score, parts), default=0)
                starts_unit = bool(parts) and _score(parts[0]) == 1
                if (re.match(rf"\s*{MARK_RE}", piece) or k > 0
                        or (cur_top >= 2 and parts and (starts_unit or (cur_top == 3 and new_top == 3)))):
                    close()
            if cur is None:
                cur = {"texts": [], "parts": [], "labeled": False}
            cur["texts"].append(piece)
            cur["parts"].extend(parts)
            cur["labeled"] = cur["labeled"] or labeled
    close()

    for d in domains:  # attach each email domain to the organization it abbreviates
        reg = registrable(d)
        label = reg.split(".")[0]
        for o in orgs:
            compact = re.sub(r"[^a-z0-9]", "", _fold(o.name))
            initials = "".join(w[0] for w in re.findall(r"[a-z0-9]+", _fold(o.name)) if w not in STOP)
            if len(label) >= 3 and (label in compact or label == initials):
                o.domain = o.domain or reg
                break
    return orgs
