"""Everything known as of one date, loaded into memory and indexed.

Only facts whose document arrived by the end of the as_of day (UTC) are loaded, plus feedback decided by then and
reconciler links whose inputs had all arrived. Nothing later can leak into a report for an earlier date.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .. import normalize as norm
from ..store import KEY_COLUMNS, Fact, Store

_NORMALISERS = {"contract_id": norm.code, "invoice_id": norm.invoice_id, "awb": norm.awb, "lane": norm.lane,
                "party": norm.code, "person_email": norm.email, "period": lambda x: str(x).strip()}


def dec(x: Any) -> Decimal | None:
    if x is None or x == "":
        return None
    try:
        return Decimal(str(x))
    except InvalidOperation:
        return None


def day(x: Any) -> date | None:
    if not x:
        return None
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def domain(email: str | None) -> str:
    return email.rsplit("@", 1)[-1].lower() if email and "@" in email else ""


@dataclass(frozen=True)
class DocInfo:
    doc_id: int
    doc_ref: str
    format: str
    filename: str | None
    received_at: str
    doc_type: str | None
    is_scan: bool
    parent_id: int | None
    from_addr: str | None       # the email's sender, or for an attachment the sender of its email
    from_name: str | None
    subject: str | None


class View:
    def __init__(self, as_of: str | None, facts: list[Fact], docs: dict[int, DocInfo], feedback: list[dict],
                 links: dict[str, dict]):
        self.as_of = as_of
        self.as_of_date = day(as_of)
        self.facts = facts
        self.docs = docs
        self.feedback = feedback
        self.links = links
        self._by_kind: dict[str, list[Fact]] = defaultdict(list)
        self._index: dict[tuple, list[Fact]] = defaultdict(list)
        for f in facts:
            self._by_kind[f.kind].append(f)
            for col in KEY_COLUMNS:
                val = getattr(f, col)
                if val is not None:
                    self._index[(f.kind, col, val)].append(f)

    @classmethod
    def load(cls, store: Store, as_of: str | None = None) -> "View":
        cutoff = norm.end_of_day(as_of) if as_of else "9999-12-31T23:59:59Z"
        rows = store.con.execute(
            "SELECT d.id, d.doc_ref, d.format, d.filename, d.received_at, d.doc_type, d.is_scan, d.parent_id, "
            "COALESCE(e.from_addr, pe.from_addr) AS from_addr, COALESCE(e.from_name, pe.from_name) AS from_name, "
            "COALESCE(e.subject, pe.subject) AS subject FROM documents d "
            "LEFT JOIN emails e ON e.doc_id = d.id LEFT JOIN emails pe ON pe.doc_id = d.parent_id "
            "WHERE d.received_at <= ?", (cutoff,)).fetchall()
        docs = {r["id"]: DocInfo(r["id"], r["doc_ref"], r["format"], r["filename"], r["received_at"], r["doc_type"],
                                 bool(r["is_scan"]), r["parent_id"], r["from_addr"], r["from_name"], r["subject"])
                for r in rows}
        links = {"label_code": dict(store.links("label_code", as_of=as_of))}
        return cls(as_of, store.facts(as_of=as_of), docs, store.feedback_items(as_of=as_of), links)

    def without_feedback(self) -> "View":
        """The same documents with no human decisions: what we'd say if nobody had given feedback."""
        return View(self.as_of, self.facts, self.docs, [], self.links)

    # ---- lookups ----------------------------------------------------------------------------------------------
    def kind(self, kind: str) -> list[Fact]:
        return self._by_kind.get(kind, [])

    def get(self, kind: str, **keys: Any) -> list[Fact]:
        """Facts of a kind matching every given key (normalised the way the store normalises them)."""
        if not keys:
            return self.kind(kind)
        items = [(k, _NORMALISERS[k](v)) for k, v in keys.items() if v is not None]
        if not items:
            return self.kind(kind)
        (k0, v0), rest = items[0], items[1:]
        return [f for f in self._index.get((kind, k0, v0), []) if all(getattr(f, k) == v for k, v in rest)]

    def doc(self, doc_id: int) -> DocInfo | None:
        return self.docs.get(doc_id)

    def from_same_doc(self, fact: Fact, kind: str) -> list[Fact]:
        return [f for f in self.kind(kind) if f.doc_id == fact.doc_id]

    def sender(self, fact: Fact) -> tuple[str | None, str | None]:
        """(address, display name) of the email the fact came from."""
        info = self.doc(fact.doc_id)
        return (info.from_addr, info.from_name) if info else (None, None)

    def contracts(self) -> list[str]:
        return sorted({f.contract_id for f in self.kind("instrument") if f.contract_id})


def latest(facts: Iterable[Fact]) -> Fact | None:
    """The most recently received fact (ties: the later one stored)."""
    facts = list(facts)
    return max(facts, key=lambda f: (f.known_at, f.id)) if facts else None
