"""Rule-based affiliation parsing of a paper's author block (no LLM).

Input: the author block as text lines (from arXiv HTML or page 1 of the PDF)
and the arXiv metadata author list. Output: organizations with unit,
email domain and the authors that belong to each.

  1. email domains are collected; the title (lines before the first author
     name), notes (correspondence, equal contribution, funding, ...) and
     everything from "Abstract"/"Keywords"/"Figure 1" on are dropped;
  2. author names are cut out of each line, keeping their markers (^1, ², *, †);
  3. what is left is grouped into affiliations: a new one starts at a leading
     marker, after ";", after an author name, or when a line starts a new
     organization (a "Department of ..." line after a complete affiliation);
  4. each affiliation's comma parts are scored: top-level organizations
     (University ..., ... Inc., well-known companies) take the parts before
     them as their unit (Department of ..., ... Lab); addresses are dropped;
  5. authors are linked to organizations by matching markers, or else by
     position (the names that come right before an affiliation).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Sequence

from .models import Org

SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
SYMBOLS = "*†‡§¶#♯∗⋆✝♮♭♢♠♣♡"
MARK_RE = rf"(?:\^?(?:\d{{1,2}}|[{SYMBOLS}]))"
LOWER = "a-zß-öø-ÿ"  # a lowercase letter right after a name means it is not the end of the name

# -- vocabularies -------------------------------------------------------------

UNIV_RE = re.compile(  # unambiguous top-level academic organizations
    r"universit|\bcollege\b(?!\s+of\b)|institute of (?:science and )?technology|polytechnic|politecnico|polytechnique|"
    r"école|\becole\b|hochschule|medical school|school of (?:medicine|public health)\b|"
    r"\btu (?:delft|wien|berlin|munich|münchen|darmstadt|dresden|eindhoven|dortmund|braunschweig)\b|"
    r"\b(?:kaist|kaust|epfl|eth zurich|eth zürich|ucl|ucla|ucsd|nyu|csail)\b", re.I)
ACADEMIA_RE = re.compile(
    r"universit|college|polytechnic|école|ecole|hochschule|institut\w* (?:of|for|de|für) |academy of sciences|"
    r"medical school|\b(?:cnrs|inria|max planck|riken|a\*star|mila|vector institute|fraunhofer|"
    r"kaist|kaust|epfl|eth|csail)\b", re.I)
COMPANY_RE = re.compile(
    r"(\binc\b\.?|\bltd\b\.?|\bllc\b|\bcorp\b\.?|\bcorporation\b|\bgmbh\b|\bplc\b|\bco\.,? ?ltd|\bpvt\b|"
    r"\btechnologies\b|\btechnology$|\bconsulting\b|\bsolutions\b|\bs\.?a\.?s?\.?$|\bai lab\b$)", re.I)
GOV_RE = re.compile(
    r"\b(ministry|government|national (?:laborator\w*|lab|institutes? of health)|agency|bureau|world bank|"
    r"united nations|unicef|oecd|european commission|nasa|nist|nih|darpa|army|navy|air force|"
    r"federal reserve|central bank|academy of military)", re.I)
NONPROFIT_RE = re.compile(r"\b(foundation|allen institute|non-?profit|ngo\b|charit\w*)", re.I)
ORG_RE = re.compile(
    r"\b(institut\w*|center|centre|laborator\w*|labs?|research|academy|hospital|clinic|council|"
    r"observatory|museum|society)\b", re.I)
UNIT_RE = re.compile(  # parts inside an organization
    r"^(?:the )?(dep(?:artment|t)\b\.?|dept\b|school of|faculty|division|chair\b|college of|graduate school|"
    r"key lab|state key|national key|lab(?:oratory)? (?:of|for)|program(?:me)? (?:in|of)|"
    r".* (?:lab|laboratory|group|team|unit|program|programme)$)", re.I)
TITLE_RE = re.compile(  # job titles in affiliations ("Researcher, Global Product Strategy, Roche")
    r"^(?:(?:assistant|associate|adjunct|full|senior|principal|lead|chief|staff|distinguished|visiting|"
    r"vice)\s+)*(professor|prof\.?|researcher|research (?:scientist|engineer|intern|fellow|assistant)|scientist|"
    r"data scientist|engineer|student|ph\.?d\.?(?: student| candidate)?|postdoc\w*|director|president|manager|"
    r"intern|fellow|lecturer|md|m\.d\.?|ceo|cto|founder|co-founder|head(?: of .*)?|member)\b", re.I)
SUFFIX_RE = re.compile(r"^(inc|llc|ltd|gmbh|corp|co|plc|s\.?a|ag|pvt\.? ltd|limited|l\.?p)\.?$", re.I)

# Well-known organizations: pattern -> canonical name. Checked before the generic rules.
KNOWN = [(re.compile(p, re.I), n) for p, n in [
    (r"google deepmind|\bdeepmind\b", "Google DeepMind"),
    (r"\bgoogle\b", "Google"),
    (r"\bmicrosoft\b", "Microsoft"),
    (r"\bmeta\b(?![ -]?(?:analysis|learning|data|heuristic))|\bfacebook\b|\bfair\b", "Meta"),
    (r"\bamazon\b|\baws\b", "Amazon"),
    (r"\bapple\b", "Apple"),
    (r"\bnvidia\b", "NVIDIA"),
    (r"\bibm\b", "IBM"),
    (r"\bopenai\b", "OpenAI"),
    (r"\banthropic\b", "Anthropic"),
    (r"\bsalesforce\b", "Salesforce"),
    (r"\badobe\b", "Adobe"),
    (r"\bintel\b(?!li)", "Intel"),
    (r"\bsamsung\b", "Samsung"),
    (r"\bhuawei\b", "Huawei"),
    (r"\balibaba\b|\bdamo academy\b|\bant group\b|\btongyi\b|\bqwen\b", "Alibaba"),
    (r"\btencent\b", "Tencent"),
    (r"\bbaidu\b", "Baidu"),
    (r"\bbytedance\b|\btiktok\b", "ByteDance"),
    (r"\bjd\.com\b|\bjd explore\b", "JD.com"),
    (r"\bxiaomi\b", "Xiaomi"),
    (r"\bkuaishou\b", "Kuaishou"),
    (r"\bmeituan\b", "Meituan"),
    (r"\bnetflix\b", "Netflix"),
    (r"\bspotify\b", "Spotify"),
    (r"\blinkedin\b", "LinkedIn"),
    (r"\buber\b", "Uber"),
    (r"\bairbnb\b", "Airbnb"),
    (r"\bpinterest\b", "Pinterest"),
    (r"\bsnap inc|\bsnapchat\b", "Snap"),
    (r"\bbloomberg\b", "Bloomberg"),
    (r"\bqualcomm\b", "Qualcomm"),
    (r"\bsony\b", "Sony"),
    (r"\bnaver\b", "NAVER"),
    (r"\bmistral ai\b", "Mistral AI"),
    (r"\bcohere\b", "Cohere"),
    (r"\bhugging ?face\b", "Hugging Face"),
    (r"\bservicenow\b", "ServiceNow"),
    (r"\boracle\b", "Oracle"),
    (r"\bsap\b", "SAP"),
    (r"\bsiemens\b", "Siemens"),
    (r"\bbosch\b", "Bosch"),
    (r"\bdell\b", "Dell Technologies"),
    (r"\bworld bank\b", "World Bank Group"),
    (r"\ballen institute for (?:ai|artificial intelligence)\b|\bai2\b", "Allen Institute for AI"),
    (r"\bmassachusetts institute of technology\b|^mit$|^mit csail$|^csail$", "Massachusetts Institute of Technology"),
    (r"\bcarnegie mellon\b|^cmu$", "Carnegie Mellon University"),
    (r"\bshanghai (?:ai|artificial intelligence) lab", "Shanghai AI Laboratory"),
    (r"\bku ?leuven\b", "KU Leuven"),
    (r"^tno$", "TNO"),
    (r"\bcentralesup[ée]lec\b", "CentraleSupélec"),
    (r"^(?:universit[ée] )?paris[- ]saclay$", "Université Paris-Saclay"),
]]
GENERIC_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "qq.com",
                "163.com", "126.com", "foxmail.com", "icloud.com", "me.com", "protonmail.com", "proton.me",
                "mail.com", "aol.com", "yandex.ru", "sina.com", "naver.com", "gmx.de", "web.de", "hotmail.co.uk",
                "yahoo.co.jp", "outlook.jp", "example.com"}
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
END_RE = re.compile(  # the author block is over
    r"^\W*(keywords?|index terms|ccs concepts|acm reference|abstract|introduction|figure \d|fig\. ?\d|table \d|©)",
    re.I)
LABEL_RE = re.compile(r"^\s*(affiliations?|address|institutions?)\s*:\s*", re.I)
SKIP_LABEL_RE = re.compile(r"^\s*(e-?mails?|note|orcid|homepage|web|url|phone|tel|voice|fax|contact)\b\s*:?", re.I)
EMAIL_RE = re.compile(r"(?:\{[^}]{1,300}\}|[\w.+\-]+)\s?@\s?([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)", re.I)
STOP = {"of", "the", "and", "for", "de", "la", "di", "der", "für", "at", "in", "&"}


# -- helpers ------------------------------------------------------------------

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
    return s.replace("( ", "(").replace(" )", ")")


def registrable(domain: str) -> str:
    parts = domain.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domains_in(text: str) -> list[str]:
    out = []
    for m in EMAIL_RE.finditer(text):
        d = m.group(1).lower().rstrip(".")
        if d not in out:
            out.append(d)
    return out


def _known(s: str) -> str | None:
    for rx, name in KNOWN:
        if rx.search(s):
            return name
    return None


def _is_address(part: str) -> bool:
    p = _fold(part).strip(" .")
    return (p in COUNTRIES or bool(re.search(r"\d{3,}", p)) or bool(re.fullmatch(r"[a-z]{2}", p))
            or bool(re.search(r"\b(street|st\.|road|rd\.|avenue|ave\.|blvd|floor|room|building|campus|p\.?o\.? box|"
                              r"district|province|zip)\b", p)))


def _org_score(part: str) -> int:
    """5 known organization · 4 university/company · 3 other organization keyword ·
    2 institute/center/lab · 1 unit (department, lab, school of ...) · 0 nothing."""
    if UNIT_RE.match(part) and not _known(part):
        return 1
    if _known(part):
        return 5
    if UNIV_RE.search(part) or COMPANY_RE.search(part):
        return 4
    if GOV_RE.search(part) or NONPROFIT_RE.search(part) or ACADEMIA_RE.search(part):
        return 3
    if ORG_RE.search(part):
        return 2
    return 0


def _parts(seg: str) -> tuple[list[str], bool, set[int]]:
    """Comma parts of an affiliation without addresses and job titles; whether an address was seen;
    indices of parts split off "A University & B Institute" (organizations in their own right)."""
    out: list[str] = []
    address = False
    split: set[int] = set()
    for p in re.split(r",|\s+-\s+|\s+–\s+|\|", LABEL_RE.sub("", seg)):
        p = _clean(p)
        if not p or not re.search(r"[^\W\d_]{2}", p):
            continue
        if SUFFIX_RE.match(p) and out:
            out[-1] = f"{out[-1]}, {p}"  # "Ankura Consulting Group, LLC"
        elif _is_address(p):
            address = True
        elif not (TITLE_RE.match(p) and _org_score(p) <= 1):
            # "Aalto University & ELLIS Institute": two organizations
            halves = re.split(r"\s+(?:&|and)\s+", p)
            if len(halves) == 2 and all(_org_score(h) >= 2 and len(h.split()) >= 2 for h in halves):
                out.extend(halves)
                split.update({len(out) - 2, len(out) - 1})
            else:
                out.append(p)
    return out, address, split


def parse_segment(seg: str, labeled: bool = False) -> list[Org]:
    """Organizations in one affiliation, e.g. "Development Impact AI Lab, World Bank Group" or
    "Dept. of CS, School of Medicine, Oregon Health & Science University, Portland, OR, USA".
    Each top-level organization takes the parts before it as its unit."""
    parts, address, split = _parts(seg)
    if not parts:
        return []
    scores = [max(_org_score(p), 3 if i in split else 0) for i, p in enumerate(parts)]
    top = max(scores)
    if top >= 3:
        anchors = [i for i, sc in enumerate(scores) if sc >= 3]
    elif top >= 1:
        anchors = [max(i for i, sc in enumerate(scores) if sc == top)]
    elif labeled and not (address and len(parts) == 1 and len(parts[0].split()) <= 3):
        # marked as an affiliation but no keyword, e.g. "Affiliation: Illuin Technology";
        # a short name next to a country is a city ("Affiliation: London, UK")
        anchors = [len(parts) - 1]
    else:
        return []
    out, prev = [], -1
    for a in anchors:
        units = parts[prev + 1:a][-2:]
        prev = a
        org, unit = parts[a], ", ".join(units)
        name = _known(org)
        if name:
            if _fold(org) not in _fold(name) and (_fold(name) not in _fold(org) or len(org) > len(name) + 12):
                unit = unit or org  # "Amazon Web Services" -> Amazon / Amazon Web Services
            org = name
        if len(org) <= 120 and len(org.split()) <= 14:
            out.append(Org(name=org, unit=unit[:160]))
    return out


def _name_patterns(name: str) -> list[re.Pattern]:
    """Patterns for an author's name, most specific first: full name as written (case-sensitive,
    tolerating names glued together like "PresLaura Ruis" and markers like "Heckman2"), full name
    in any case ("JOHN SMITH"), surname alone. Middle names and initials are optional."""
    toks = re.findall(r"[^\W\d_]+", name)
    if not toks:
        return []
    first, sur = re.escape(toks[0]), re.escape(toks[-1])
    mid = r"(?:[\s.\-]+[^\W\d_][\w.\-]*?){0,3}?[\s.\-]*"
    glue = r"(?:[a-z]{1,4}(?=\d))?"  # LaTeX label residue: "Liucor1"
    end = rf"(?![{LOWER}])"
    pats = []
    if len(toks) > 1:
        pats.append(re.compile(rf"(?<![A-Z]){first}{end}{mid}{sur}{glue}{end}"))
        pats.append(re.compile(rf"(?<![^\W\d_]){first}{end}{mid}{sur}{glue}{end}", re.I))
    pats.append(re.compile(rf"(?<![^\W\d_]){sur}{end}", re.I))
    return pats


def _marks(s: str) -> set[str]:
    return {m.lstrip("^") for m in re.findall(MARK_RE, s)}


# -- main ---------------------------------------------------------------------

def parse_block(text: str, authors: Sequence[str]) -> list[Org]:
    text = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+", lambda m: "^" + m.group().translate(SUPERSCRIPTS) + " ", text)
    domains = [d for d in domains_in(text) if registrable(d) not in GENERIC_MAIL]
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # each author is matched by full name if it occurs anywhere, else by surname
    pats = []
    for a in authors:  # most specific pattern that matches within a line (names can wrap in PDFs)
        cands = _name_patterns(a)
        p = next((c for c in cands if any(c.search(l) for l in lines)), None)
        if p is not None:
            pats.append((a, p))
    first = next((i for i, l in enumerate(lines) if any(p.search(l) for _, p in pats)), 0)
    lines = lines[first:]  # the title comes before the first author

    orgs: list[Org] = []
    org_marks: list[set[str]] = []
    author_marks: dict[str, set[str]] = {}
    positional: list[tuple[list[str], int]] = []  # (authors right before an affiliation, org index)
    pending: list[str] = []
    in_affil = False
    cur: dict | None = None  # affiliation being collected over consecutive lines

    def close() -> None:
        nonlocal cur, in_affil
        if not cur:
            return
        for org in parse_segment(" , ".join(cur["texts"]), cur["labeled"]):
            idx = next((k for k, o in enumerate(orgs) if _fold(o.name) == _fold(org.name)), None)
            if idx is None:
                orgs.append(org)
                org_marks.append(set())
                idx = len(orgs) - 1
            elif org.unit and _fold(org.unit) not in _fold(orgs[idx].unit):
                orgs[idx].unit = "; ".join(x for x in (orgs[idx].unit, org.unit) if x)[:160]
            org_marks[idx].update(cur["marks"])
            if (pending, idx) not in positional:
                positional.append((list(pending), idx))
            in_affil = True
        cur = None

    for line in lines:
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
        labeled = bool(LABEL_RE.match(line))
        line = LABEL_RE.sub("", line)

        # cut out author names, keeping the markers right after each name
        found = []
        for a, p in pats:
            for m in p.finditer(line):
                tail = re.match(rf"\s*((?:{MARK_RE}[\s,]*)+)", line[m.end():])
                found.append((m.start(), m.end() + (tail.end() if tail else 0), a, tail.group(1) if tail else ""))
        found.sort(key=lambda f: (f[0], -f[1]))
        rest, pos, here = [], 0, []
        for st, en, a, mk in found:
            if st < pos:
                continue
            rest.append(line[pos:st])
            pos = en
            here.append(a)
            if mk:
                author_marks.setdefault(a, set()).update(_marks(mk))
        rest.append(line[pos:])
        if here:
            close()
            if in_affil:
                pending, in_affil = [], False
            pending.extend(x for x in here if x not in pending)

        residue = " , ".join(rest)
        pieces = re.split(rf"(?:(?<=\s)|^|(?<=,))(?=\^?(?:\d{{1,2}}|[{SYMBOLS}])\s*[A-Z])|;", residue)
        for k, piece in enumerate(pieces):
            if not re.search(r"[^\W\d_]{2}", piece) or _clean(piece).lower() in STOP | {"with", "et al"}:
                continue
            mk = re.match(rf"\s*((?:{MARK_RE}[\s,]*)+)", piece)
            parts = _parts(piece)[0]
            if cur is not None:
                cur_top = max([_org_score(p) for p in cur["parts"]], default=0)
                new_top = max([_org_score(p) for p in parts], default=0)
                starts_unit = bool(parts) and _org_score(parts[0]) == 1
                # a new affiliation: a marker, ";", or a line that starts another organization
                if mk or k > 0 or (cur_top >= 2 and parts and (starts_unit or (cur_top >= 3 and new_top >= 3))):
                    close()
            if cur is None:
                cur = {"texts": [], "parts": [], "marks": set(), "labeled": False}
            cur["texts"].append(piece)
            cur["parts"].extend(parts)
            cur["labeled"] = cur["labeled"] or labeled or bool(with_)
            if mk:
                cur["marks"].update(_marks(mk.group(1)))
    close()

    # domains: attach to the organization they abbreviate
    for d in domains:
        reg = registrable(d)
        label = reg.split(".")[0]
        for o in orgs:
            compact = re.sub(r"[^a-z0-9]", "", _fold(o.name))
            initials = "".join(w[0] for w in re.findall(r"[a-z0-9]+", _fold(o.name)) if w not in STOP)
            if len(label) >= 3 and (label in compact or label == initials):
                o.domain = o.domain or reg
                break

    # authors: by markers when both sides have them, else by position
    if author_marks:
        for i, o in enumerate(orgs):
            if org_marks[i]:
                o.authors = [a for a in authors if author_marks.get(a, set()) & org_marks[i]]
    if not any(o.authors for o in orgs):
        for names, idx in positional:
            orgs[idx].authors.extend(a for a in names if a not in orgs[idx].authors)
        named = [o for o in orgs if o.authors]
        if len(named) <= 1 and len({idx for _, idx in positional}) <= 1 and orgs:
            # a single affiliation (listed after everyone, or once): all authors
            (named or orgs)[0].authors = list(authors)
    for o in orgs:  # metadata order
        o.authors = [a for a in authors if a in o.authors]
    return orgs
