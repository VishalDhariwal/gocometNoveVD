"""Which cases go to the reviewer. Plain code: the reviewer never picks its own work.

Upgrade cases (code stayed silent, and the reviewer may turn one into a finding, under check.py's tests) come first,
largest billed amount first; then, in autonomous mode only, second looks at what is about to be executed. At most
config.REVIEW_MAX_CASES in all. One case per invoice and reason; an invoice that already has a finding of the class a
case is about isn't sent for that class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .. import normalize as norm
from ..decide.common import ZERO, Verdict, amount_of, invoice_date, lines_of
from ..knowledge.view import day
from .library import Library, compact

UPGRADE_CLASSES = ("waived_but_billed", "duplicate")
REASONS = {
    "waiver_condition": "a waiver from an authorised person sets a condition in words code can't check",
    "waiver_scope": "a waiver from an authorised person names no waybill, invoice or month that code could read",
    "quoted_waiver": "a waiver seen only quoted inside someone else's email; the original may be held",
    "unmapped_labels": "billed lines whose label isn't in Annex E and wasn't mapped, so no detector looked at them",
    "unread_email": "an email no reader could make sense of names this invoice or its waybills",
    "second_look": "a finding about to be executed with no person looking",
}
WAIVER_WINDOW = timedelta(days=90)
PER_WAIVER = 3


@dataclass
class Case:
    reason: str
    invoice_id: str | None
    cls: str | None                       # the class it's about; None: the reviewer says (waived or duplicate)
    power: str                            # "upgrade" or "hold"
    verdict: Verdict | None = None        # what code said, if anything
    focus_docs: list[int] = field(default_factory=list)
    focus_facts: list[int] = field(default_factory=list)
    weight: Decimal = ZERO
    contract: str | None = None
    note: str = ""                        # the specific question, in words

    @property
    def key(self) -> str:
        if self.verdict is not None:
            return self.verdict.finding_key
        return f"{self.cls or 'open'}|{self.invoice_id}"

    @property
    def why(self) -> str:
        return REASONS[self.reason]


def pick(lib: Library, verdicts: list[Verdict], gates: dict, mode: str, limit: int) -> list[Case]:
    view, terms = lib.view, lib.terms
    by_key = {v.finding_key: v for v in verdicts}
    claimed = {line: v.cls for v in verdicts if v.verdict == "finding" for line in v.lines}
    cases: dict[tuple, Case] = {}

    def has_finding(cls: str, inv: str) -> bool:
        v = by_key.get(norm.finding_key({"class": cls, "invoice_id": inv}))
        return v is not None and v.verdict == "finding"

    def free_lines(inv: str) -> list:
        h = lib.headers.get(inv)
        return [f for f in lines_of(view, h) if f.id not in claimed and amount_of(f) > 0] if h else []

    def add(c: Case) -> None:
        if c.invoice_id and c.cls and has_finding(c.cls, c.invoice_id):
            return
        h = lib.headers.get(c.invoice_id) if c.invoice_id else None
        if h is not None and h.doc_id not in c.focus_docs:
            c.focus_docs.append(h.doc_id)
        c.contract = c.contract or lib.contract.get(c.invoice_id or "")
        cases.setdefault((c.invoice_id, c.cls, c.reason), c)

    # 1. waiver questions code left open (decide/waiver.py records them on its verdict)
    for v in verdicts:
        if v.cls != "waived_but_billed" or v.verdict == "finding":
            continue
        for q in v.checks.get("open") or []:
            w = lib.facts_by_id.get(q.get("waiver"))
            if w is None:
                continue
            lines = [lib.facts_by_id[i] for i in q.get("lines") or [] if i in lib.facts_by_id and i not in claimed]
            if not lines:
                continue
            note = (f"The waiver's condition, as the reader took it down: {w.payload.get('condition')!r}."
                    if q["reason"] == "waiver_condition" else "")
            add(Case(q["reason"], v.key["invoice_id"], "waived_but_billed", "upgrade", v, [w.doc_id],
                     [w.id] + [f.id for f in lines], sum((amount_of(f) for f in lines), ZERO), v.contract, note))

    # 2. waivers from an authorised person that the reader couldn't pin to a waybill, invoice or month
    for w in view.kind("waiver"):
        p = w.payload
        if p.get("from_quoted_text") or p.get("awbs") or p.get("invoices") or p.get("months"):
            continue
        said_on = day(w.known_at)
        found = []
        for inv, h in lib.headers.items():
            contract = lib.contract.get(inv)
            if not contract or has_finding("waived_but_billed", inv):
                continue
            if terms.auth.of_email_fact(w, contract, "waivers", said_on).level > 2:
                continue
            codes = {terms.charge_code(contract, c) for c in p.get("charges") or []} - {None}
            lines = [f for f in free_lines(inv) if terms.charge_code(contract, f.payload.get("label")) in codes]
            lines = [f for f in lines if _near(day(f.payload.get("pickup_date")) or invoice_date(h), said_on)]
            if lines:
                gap = min(abs(((day(f.payload.get("pickup_date")) or invoice_date(h) or said_on) - said_on).days)
                          for f in lines)
                found.append((gap, inv, contract, lines))
        for _, inv, contract, lines in sorted(found)[:PER_WAIVER]:
            add(Case("waiver_scope", inv, "waived_but_billed", "upgrade",
                     by_key.get(norm.finding_key({"class": "waived_but_billed", "invoice_id": inv})),
                     [w.doc_id], [w.id] + [f.id for f in lines], sum((amount_of(f) for f in lines), ZERO), contract,
                     f"The waiver as the reader took it down: charges {p.get('charges')}, scope words "
                     f"{p.get('scope_words')!r}."))

    # 3. labels nobody mapped: no detector could see these lines
    for inv in sorted(lib.headers):
        contract = lib.contract.get(inv)
        lines = [f for f in free_lines(inv) if terms.charge_code(contract, f.payload.get("label")) is None]
        if lines:
            add(Case("unmapped_labels", inv, None, "upgrade", None, [], [f.id for f in lines],
                     sum((amount_of(f) for f in lines), ZERO), contract,
                     "Unmapped labels: " + "; ".join(sorted({str(f.payload.get('label')) for f in lines}))))

    # 4. emails no reader understood that name one of our invoices or its waybills
    read = {f.doc_id for f in view.facts}
    children: dict[int, list[int]] = {}
    for d in lib.docs.values():
        if d.parent_id is not None:
            children.setdefault(d.parent_id, []).append(d.doc_id)
    names = {}
    for inv, h in lib.headers.items():
        names[compact(inv)] = inv
        for f in lines_of(view, h):
            if f.awb and len(compact(f.awb)) >= 6:
                names.setdefault(compact(f.awb), inv)
    for doc_id, em in sorted(lib.emails.items()):
        if doc_id in read or any(c in read for c in children.get(doc_id, [])):
            continue
        text = compact(em["own"])
        hit = sorted({inv for name, inv in names.items() if name and name in text})
        for inv in hit[:2]:
            lines = free_lines(inv)
            if lines:
                add(Case("unread_email", inv, None, "upgrade", None, [doc_id], [f.id for f in lines],
                         sum((amount_of(f) for f in lines), ZERO)))

    upgrades = sorted(cases.values(), key=lambda c: (-c.weight, c.invoice_id or "", c.reason))

    # 5. autonomous mode: a second look at whatever is about to go out with nobody looking
    holds = []
    if mode == "autonomous":
        for v in verdicts:
            gate = gates.get(v.finding_key)
            if v.verdict == "finding" and gate is not None and gate.action.startswith("execute:"):
                docs = [s.fact.doc_id for s in v.evidence][:4]
                holds.append(Case("second_look", v.key.get("invoice_id"), v.cls, "hold", v, docs,
                                  [s.fact.id for s in v.evidence], v.amount_usd or ZERO, v.contract))
        holds.sort(key=lambda c: (-c.weight, c.key))
    return (upgrades + holds)[:max(0, limit)]


def _near(d: date | None, said_on: date | None) -> bool:
    return d is not None and said_on is not None and abs(d - said_on) <= WAIVER_WINDOW
