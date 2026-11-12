"""Initial index: import the official arXiv metadata snapshot from Kaggle
(https://www.kaggle.com/datasets/Cornell-University/arxiv), one JSON object per
line, read straight from the downloaded .zip."""
from __future__ import annotations

import email.utils
import io
import json
import logging
import sqlite3
import zipfile
from typing import Iterator, Sequence, TextIO

from . import db
from .models import Paper, norm

log = logging.getLogger(__name__)


def _open(path: str) -> TextIO:
    if str(path).endswith(".zip"):
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.endswith(".json"))
        return io.TextIOWrapper(z.open(name), encoding="utf-8")
    return open(path, encoding="utf-8")


def _iso(rfc: str | None) -> str | None:
    try:
        return email.utils.parsedate_to_datetime(rfc).date().isoformat() if rfc else None
    except (TypeError, ValueError):
        return None


def parse_record(r: dict) -> Paper:
    vers = r.get("versions") or []
    authors = [" ".join(x for x in (a[1], a[0], a[2] if len(a) > 2 else "") if x)
               for a in (r.get("authors_parsed") or [])]
    return Paper(
        id=r["id"], title=norm(r.get("title")) or "", abstract=norm(r.get("abstract")) or "",
        authors=authors, categories=(r.get("categories") or "").split(),
        created=_iso(vers[0]["created"]) if vers else None,
        updated=_iso(vers[-1]["created"]) if len(vers) > 1 else None,
        license=r.get("license"), doi=r.get("doi"), journal_ref=norm(r.get("journal-ref")),
        comments=norm(r.get("comments")), datestamp=r.get("update_date"))


def iter_snapshot(path: str, categories: Sequence[str]) -> Iterator[Paper]:
    cats = set(categories)
    with _open(path) as f:
        for line in f:
            r = json.loads(line)
            if cats & set((r.get("categories") or "").split()):
                yield parse_record(r)


def import_snapshot(con: sqlite3.Connection, path: str, categories: Sequence[str], batch: int = 5000) -> int:
    n, buf = 0, []
    for p in iter_snapshot(path, categories):
        buf.append(p)
        if len(buf) >= batch:
            db.upsert_papers(con, buf, added_at=None)
            n += len(buf)
            buf.clear()
            if n % 50000 == 0:
                log.info("imported %d papers", n)
    db.upsert_papers(con, buf, added_at=None)
    n += len(buf)
    log.info("import done: %d papers", n)
    return n
