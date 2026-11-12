from __future__ import annotations

from dataclasses import dataclass, field


def norm(s: str | None) -> str | None:
    """Collapse whitespace (arXiv titles/abstracts contain hard line breaks)."""
    return " ".join(s.split()) if s else None


@dataclass
class Paper:
    id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    created: str | None  # YYYY-MM-DD of v1
    updated: str | None = None  # YYYY-MM-DD of latest version, if > v1
    license: str | None = None
    doi: str | None = None
    journal_ref: str | None = None
    comments: str | None = None
    datestamp: str | None = None

    @property
    def primary_cat(self) -> str | None:
        return self.categories[0] if self.categories else None

    @property
    def abs_url(self) -> str:
        return f"https://arxiv.org/abs/{self.id}"


@dataclass
class Org:
    name: str
    unit: str = ""
    domain: str = ""
