"""What every detector shares: the Verdict it returns, the claim ledger, money in USD, and small lookups.

A Verdict is one answer for one finding key. Phase 5 turns it into a report item: it picks the action from `remedy`
and `checks`, scores confidence, and writes ids, evidence quotes and supersedes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .. import normalize as norm
from ..knowledge.pricing import contract_for
from ..knowledge.terms import Decided, Missing, Result, Source, Terms, Undefined
from ..knowledge.view import View, dec, day
from ..money import pct_of, to_cents
from ..store import Fact

ZERO = Decimal("0")
VERDICTS = ("finding", "no_finding", "conflict", "cannot_determine")
# remedies: dispute | claim_credit | request_document (outbound); escalate | alert | payment_reminder | track_credit
OUTBOUND = {"dispute", "claim_credit", "request_document"}


@dataclass
class Verdict:
    cls: str
    key: dict
    verdict: str
    amount_usd: Decimal | None = None
    amount_range: tuple[Decimal, Decimal] | None = None
    deadline: str | None = None
    missing_doc: dict | None = None
    remedy: str | None = None
    evidence: list[Source] = field(default_factory=list)
    why: str = ""
    steps: list[str] = field(default_factory=list)
    contract: str | None = None
    currency: str | None = None
    amount_native: Decimal | None = None
    lines: list[int] = field(default_factory=list)       # billed line facts this verdict claims
    feedback: list[str] = field(default_factory=list)    # feedback items that shaped it (for learned_at)
    checks: dict = field(default_factory=dict)           # what the unattended-action test needs

    @property
    def finding_key(self) -> str:
        return norm.finding_key({"class": self.cls, **self.key})

    def cite(self, *sources: Source | None) -> "Verdict":
        for s in sources:
            if s is not None and s.fact.quote and all(e.fact.id != s.fact.id for e in self.evidence):
                self.evidence.append(s)
        return self

    def learn(self, *ids: str | None) -> "Verdict":
        self.feedback = sorted(set(self.feedback) | {i for i in ids if i})
        return self


class Claims:
    """Each billed line can be claimed by one class only. Detectors run in priority order (duplicate, waived,
    late fee, overcharge), so the first claim wins and a line is never disputed twice."""

    def __init__(self) -> None:
        self.by_line: dict[int, str] = {}

    def taken(self, line_id: int) -> str | None:
        return self.by_line.get(line_id)

    def claim(self, cls: str, line_ids) -> None:
        for i in line_ids:
            self.by_line.setdefault(i, cls)


# ---- lookups --------------------------------------------------------------------------------------------------
def invoice_headers(view: View) -> dict[str, Fact]:
    """invoice_id -> the first copy we received of each invoice or statement (credit notes excluded)."""
    out: dict[str, Fact] = {}
    for f in sorted(view.kind("invoice"), key=lambda f: (f.known_at, f.id)):
        if f.payload.get("doc_kind") != "credit_note" and f.invoice_id not in out:
            out[f.invoice_id] = f
    return out


def lines_of(view: View, header: Fact) -> list[Fact]:
    lines = [f for f in view.from_same_doc(header, "invoice_line") if f.invoice_id == header.invoice_id]
    return sorted(lines, key=lambda f: (f.payload.get("line_no") or 0, f.id))


def contract_of(terms: Terms, header: Fact) -> str | None:
    return contract_for(terms, header, lines_of(terms.view, header))[0]


def invoice_date(header: Fact) -> date | None:
    return day(header.payload.get("document_date")) or day(header.known_at)


def latest(facts) -> Fact | None:
    facts = list(facts)
    return max(facts, key=lambda f: (f.known_at, f.id)) if facts else None


def ledger(view: View, invoice_id: str) -> Fact | None:
    """The newest payment-register row we hold for this invoice."""
    return latest(view.get("ledger_entry", invoice_id=invoice_id))


def credit_notes(view: View, contract: str | None) -> list[Fact]:
    return [f for f in view.kind("credit_note") if contract is None or f.contract_id in (None, contract)]


def missing_doc(m: Missing) -> dict:
    h = m.holder or {}
    return {"name": m.name, "holder": {k: h[k] for k in ("party", "person", "role") if h.get(k)}}


def to_usd(terms: Terms, contract: str | None, currency: str | None, amount: Decimal | None,
           on: date | None) -> tuple[Decimal | None, Result | None]:
    """Convert once per finding at the Annex F rate for the month of the invoice date. -> (USD, the fx answer);
    USD is None when the rate is missing (the fx answer then says why)."""
    if amount is None:
        return None, None
    if not currency or currency.upper() == "USD":
        return to_cents(amount), None
    if contract is None or on is None:
        return None, Undefined("no contract or invoice date to convert with")
    fx = terms.fx(contract, norm.month(on))
    if isinstance(fx, Decided) and fx.value:
        return to_cents(amount / fx.value), fx
    return None, fx


# ---- dates ----------------------------------------------------------------------------------------------------
def quarter_of(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def quarter_bounds(q: str) -> tuple[date, date]:
    year, n = int(q[:4]), int(q[-1])
    start = date(year, 3 * n - 2, 1)
    end = date(year + (n == 4), 1 if n == 4 else 3 * n + 1, 1) - timedelta(days=1)
    return start, end


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:      # 29 February
        return d.replace(year=d.year + n, day=28)


def months_late(days_late: int) -> int:
    return math.ceil(days_late / 30) if days_late > 0 else 0


def fee_schedule(principal: Decimal, rate: Decimal, months: int, compounding: bool) -> list[Decimal]:
    """Each month's late fee, rounded half up to cents; compounding adds unpaid earlier fees to the base."""
    fees, base = [], principal
    for _ in range(months):
        fee = pct_of(base, rate)
        fees.append(fee)
        if compounding:
            base += fee
    return fees


def amount_of(f: Fact) -> Decimal:
    return dec(f.payload.get("amount")) or ZERO
