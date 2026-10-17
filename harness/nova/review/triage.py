"""Which cases go to the reviewer: only the ones code can't settle. The reviewer never picks its own work.

A case goes out when code is stuck or about to act on something it couldn't verify. Never for a clean finding, a clean
no-finding, a conflict (the glossary sends those to a person), or a supervised draft (a person reads it anyway).

  risky_execute        autonomous: a finding about to be executed rests on a read code couldn't verify
                       (a quote not found on its page, or a scanned page read by vision)
  waiver_condition     a waiver from an authorised person whose condition is in words code can't check
  waiver_scope         ... where the reader found no waybill, invoice or month
  quoted_waiver        a waiver seen only quoted: is the original held?
  unmapped_labels      billed lines whose label isn't in Annex E and wasn't mapped: no check looked at them
  unpriced_lines       billed lines code couldn't price (no delivery record, no declaration, no free days...)
  unread_email         an email no reader made sense of that names one of our invoices or waybills (an email read
                       cleanly with nothing in it for the audit is not one: "could you check this fee?" says nothing)
  unread_document      a document nothing could be read out of that names one of our invoices or waybills
  near_miss_reference  a waiver, credit note, extension or payment request whose invoice or waybill is one or two
                       characters off one of ours (a typo, or a decoy)
  uncovered_lane       a guard on findings code is sure of: an overcharge on a lane that a contract change we hold (an
                       amendment or annex update for the same contract) names in its text, while nothing read from
                       that document covers the lane (no rate, no document reference). The change may replace those
                       rates with a document we don't hold (cannot_determine), and a missed reading is invisible to code
  document_notes       a document of no standard form (readers/general.py) whose notes say something about money on
                       one of our invoices or waybills: a waiver, a credit, a payment, a moved due date, a dispute ...
                       Code acts on none of these notes; this is where they reach a decision.

Order: risky executes first (an unsafe execute is the costliest mistake), then by the money billed on the lines in
question. At most config.REVIEW_MAX_CASES.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .. import normalize as norm
from ..decide.common import ZERO, Verdict, amount_of, invoice_date, lines_of
from ..knowledge.view import day
from .library import Library, compact

REASONS = {
    "risky_execute": "a finding about to be executed with no person looking rests on a read code couldn't verify",
    "waiver_condition": "a waiver from an authorised person sets a condition in words code can't check",
    "waiver_scope": "a waiver from an authorised person names no waybill, invoice or month that code could read",
    "quoted_waiver": "a waiver seen only quoted inside someone else's email; the original may be held",
    "unmapped_labels": "billed lines whose label isn't in Annex E and wasn't mapped, so no check looked at them",
    "unpriced_lines": "billed lines code couldn't price",
    "unread_email": "an email no reader could make sense of names this invoice or its waybills",
    "unread_document": "a document nothing could be read out of names this invoice or its waybills",
    "near_miss_reference": "a document refers to an invoice or waybill one or two characters off one of ours",
    "document_notes": "a document of no standard form says something about the money on this invoice or its waybills",
    "uncovered_lane": "a contract change names the lane this overcharge was priced on, and nothing read from it covers "
                      "that lane",
}
WAIVER_WINDOW = timedelta(days=90)
PER_WAIVER = 3
UNVERIFIED = ("unverified", "vision")
NEAR_KINDS = ("waiver", "credit_note", "due_date_extension", "payment_request", "document_note")
CHANGE_TYPES = ("amendment", "annex_update")


@dataclass
class Case:
    reason: str
    invoice_id: str | None
    cls: str | None                       # the class code was looking at, if any
    verdict: Verdict | None = None        # what code said about it, if anything
    focus_docs: list[int] = field(default_factory=list)
    focus_facts: list[int] = field(default_factory=list)
    weight: Decimal = ZERO                # money billed on the lines in question
    contract: str | None = None
    note: str = ""                        # the specific question, in words
    doc_ref: str | None = None            # for a case about one document
    context: list[str] = field(default_factory=list)   # code's conclusions on the same invoice

    @property
    def key(self) -> str:
        if self.verdict is not None:
            return self.verdict.finding_key
        if self.reason in ("unread_document", "near_miss_reference", "document_notes") and self.invoice_id is None:
            return f"{self.reason}|{self.doc_ref}"
        return f"{self.cls or 'open'}|{self.invoice_id}|{self.reason}"

    @property
    def why(self) -> str:
        return REASONS[self.reason]

    @property
    def code_finding(self) -> bool:
        return self.verdict is not None and self.verdict.verdict == "finding"


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
        if c.invoice_id and c.cls and not c.code_finding and has_finding(c.cls, c.invoice_id):
            return
        h = lib.headers.get(c.invoice_id) if c.invoice_id else None
        if h is not None and h.doc_id not in c.focus_docs:
            c.focus_docs.append(h.doc_id)
        c.contract = c.contract or lib.contract.get(c.invoice_id or "")
        c.context = [f"{v.finding_key}: {v.verdict}" + (f" USD {v.amount_usd}" if v.amount_usd is not None else "")
                     + f" ({v.why})" for v in verdicts
                     if c.invoice_id and v.key.get("invoice_id") == c.invoice_id and v.verdict != "no_finding"]
        cases.setdefault((c.invoice_id, c.cls, c.reason, c.doc_ref), c)

    # autonomous: about to execute on a read code couldn't verify
    risky = []
    if mode == "autonomous":
        for v in verdicts:
            gate = gates.get(v.finding_key)
            weak = sorted({s.fact.verification for s in v.evidence if (s.fact.verification or "") in UNVERIFIED})
            if v.verdict == "finding" and gate is not None and gate.action.startswith("execute:") and weak:
                c = Case("risky_execute", v.key.get("invoice_id"), v.cls, v,
                         [s.fact.doc_id for s in v.evidence][:4], [s.fact.id for s in v.evidence],
                         v.amount_usd or ZERO, v.contract,
                         f"Part of the evidence was read as {', '.join(weak)}: check those numbers against the page.")
                add(c)
                risky.append(c)

    # waiver questions code left open (decide/waiver.py records them on its verdict)
    for v in verdicts:
        if v.cls != "waived_but_billed" or v.verdict == "finding":
            continue
        for q in v.checks.get("open") or []:
            w = lib.facts_by_id.get(q.get("waiver"))
            lines = [lib.facts_by_id[i] for i in q.get("lines") or [] if i in lib.facts_by_id and i not in claimed]
            if w is None or not lines:
                continue
            note = (f"The waiver's condition, as the reader took it down: {w.payload.get('condition')!r}."
                    if q["reason"] == "waiver_condition" else "")
            add(Case(q["reason"], v.key["invoice_id"], "waived_but_billed", v, [w.doc_id],
                     [w.id] + [f.id for f in lines], sum((amount_of(f) for f in lines), ZERO), v.contract, note))

    # authorised waivers the reader couldn't pin to a waybill, invoice or month
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
            lines = [f for f in free_lines(inv) if terms.charge_code(contract, f.payload.get("label")) in codes
                     and _near(day(f.payload.get("pickup_date")) or invoice_date(h), said_on)]
            if lines:
                gap = min(abs(((day(f.payload.get("pickup_date")) or invoice_date(h) or said_on) - said_on).days)
                          for f in lines)
                found.append((gap, inv, contract, lines))
        for _, inv, contract, lines in sorted(found)[:PER_WAIVER]:
            add(Case("waiver_scope", inv, "waived_but_billed",
                     by_key.get(norm.finding_key({"class": "waived_but_billed", "invoice_id": inv})),
                     [w.doc_id], [w.id] + [f.id for f in lines], sum((amount_of(f) for f in lines), ZERO), contract,
                     f"The waiver as the reader took it down: charges {p.get('charges')}, scope words "
                     f"{p.get('scope_words')!r}."))

    # lines no check could look at, or couldn't price
    for inv in sorted(lib.headers):
        contract = lib.contract.get(inv)
        unmapped = [f for f in free_lines(inv) if terms.charge_code(contract, f.payload.get("label")) is None]
        if unmapped:
            add(Case("unmapped_labels", inv, None, None, [], [f.id for f in unmapped],
                     sum((amount_of(f) for f in unmapped), ZERO), contract,
                     "Unmapped labels: " + "; ".join(sorted({str(f.payload.get('label')) for f in unmapped}))))
        ip = lib.pricing(inv)
        free = {f.id for f in free_lines(inv)}
        unpriced = [lp for lp in (ip.lines() if ip else []) if lp.status == "unpriceable" and lp.code
                    and lp.line.id in free]
        if unpriced:
            add(Case("unpriced_lines", inv, "overcharge",
                     by_key.get(norm.finding_key({"class": "overcharge", "invoice_id": inv})), [],
                     [lp.line.id for lp in unpriced], sum((amount_of(lp.line) for lp in unpriced), ZERO), contract,
                     "Why code couldn't price them: " + "; ".join(
                         f"{lp.line.payload.get('label')} ({lp.code}) on {lp.line.awb or inv}: {lp.why}"
                         for lp in unpriced[:6]) + "."))

    # documents nothing was read out of, that name one of our invoices or waybills
    names: dict[str, str] = {}
    for inv in lib.headers:
        for n in lib.names_of(inv):
            names.setdefault(n, inv)
    read = {f.doc_id for f in view.facts}
    for doc_id, d in sorted(lib.docs.items()):
        if doc_id in lib.injected:
            continue
        unread = lib.unread(doc_id)
        empty_email = (doc_id in lib.emails and doc_id not in read and not unread and doc_id not in lib.clean
                       and not any(c in read for c in lib.children.get(doc_id, [])))
        if not (unread or empty_email):
            continue
        text = compact(lib.emails[doc_id]["own"]) if empty_email else lib.flat.get(doc_id, "")
        for inv in sorted({inv for n, inv in names.items() if n in text})[:2]:
            lines = free_lines(inv)
            add(Case("unread_email" if empty_email else "unread_document", inv, None, None, [doc_id],
                     [f.id for f in lines], sum((amount_of(f) for f in lines), ZERO), doc_ref=d.doc_ref))

    # notes the general reader took from documents of no standard form: code acts on none of them, so every note
    # about money that names one of our invoices or waybills goes to the reviewer, one case per document and invoice
    noted: dict[tuple[int, str], list] = {}
    for f in view.kind("document_note"):
        if f.doc_id in lib.injected or f.payload.get("kind") in (None, "identifiers"):
            continue
        refs = {compact(x) for x in (f.invoice_id, f.awb) if x}
        said = compact(f.quote)
        for inv in sorted({inv for n, inv in names.items() if n in refs or (len(n) >= 6 and n in said)}):
            noted.setdefault((f.doc_id, inv), []).append(f)
    for (doc_id, inv), notes in sorted(noted.items()):
        lines = free_lines(inv)
        add(Case("document_notes", inv, None, None, [doc_id], [f.id for f in notes],
                 sum((amount_of(f) for f in lines), ZERO), lib.contract.get(inv),
                 "The reader's notes from it: " + "; ".join(_note(f) for f in notes) + ".",
                 doc_ref=lib.docs[doc_id].doc_ref if doc_id in lib.docs else None))

    # a guard on findings: an overcharge on a lane a contract change names, with nothing read from it for that lane
    changes = [doc_id for doc_id, d in lib.docs.items() if doc_id not in lib.injected and (
        d.doc_type in CHANGE_TYPES or any(f.kind == "instrument" and f.payload.get("type") in CHANGE_TYPES
                                          for f in lib.facts_by_doc.get(doc_id, [])))]
    for v in verdicts:
        if v.cls != "overcharge" or v.verdict != "finding" or not v.contract:
            continue
        on_lane: dict[str, list] = {}
        for i in v.lines:
            f = lib.facts_by_id.get(i)
            if f is not None and f.lane:
                on_lane.setdefault(f.lane, []).append(f)
        for doc_id in changes:
            facts = lib.facts_by_doc.get(doc_id, [])
            ours = {f.contract_id for f in facts if f.contract_id} or (
                {v.contract} if compact(v.contract) in lib.flat.get(doc_id, "") else set())
            if v.contract not in ours:
                continue
            for lane, lines in sorted(on_lane.items()):
                named = compact(lane) in lib.flat.get(doc_id, "")
                covered = any(f.lane == lane or compact(lane) in {compact(x) for x in _as_list(f.payload.get("lanes"))}
                              for f in facts)
                if named and not covered:
                    add(Case("uncovered_lane", v.key.get("invoice_id"), v.cls, v, [doc_id], [f.id for f in lines],
                             v.amount_usd or ZERO, v.contract,
                             f"{lib.docs[doc_id].doc_ref} names lane {lane}, but nothing read from it covers that lane "
                             f"(no rate, no document reference).", doc_ref=lib.docs[doc_id].doc_ref))

    # references one or two characters off ours: a typo worth a look, or a decoy
    for f in view.facts:
        if f.kind not in NEAR_KINDS or f.payload.get("from_quoted_text") or f.doc_id in lib.injected:
            continue
        for ref in _refs(f):
            if ref in names:
                continue           # it matches exactly: code already used it
            inv = next((names[n] for n in sorted(names) if _close(ref, n)), None)
            if inv is not None:
                add(Case("near_miss_reference", inv, None, None, [f.doc_id], [f.id],
                         sum((amount_of(x) for x in free_lines(inv)), ZERO), lib.contract.get(inv),
                         f"The {f.kind.replace('_', ' ')} refers to {ref}, which matches nothing we hold; invoice "
                         f"{inv} has a reference one or two characters away.", doc_ref=f.doc_ref))
                break

    rest = sorted((c for c in cases.values() if not any(c is r for r in risky)), key=lambda c: (-c.weight, c.key))
    return (sorted(risky, key=lambda c: (-c.weight, c.key)) + rest)[:max(0, limit)]


def _as_list(x) -> list:
    return [x] if isinstance(x, str) else list(x or []) if isinstance(x, (list, tuple)) else []


def _note(f) -> str:
    p = f.payload
    money = " ".join(str(x) for x in (p.get("amount"), p.get("currency")) if x)
    return f"{p.get('kind')}: {p.get('description') or ''}" + (f" ({money})" if money else "")


def _refs(f) -> list[str]:
    p = f.payload
    out = [compact(a) for a in p.get("awbs") or []] + [compact(i) for i in p.get("invoices") or []]
    out += [compact(i) for i in [f.invoice_id, *(p.get("refers_to_invoices") or [])] if i]
    if f.kind == "document_note" and f.awb:      # the general reader keeps a note's waybill as a key
        out.append(compact(f.awb))
    return [r for r in dict.fromkeys(out) if len(r) >= 6]


def _close(a: str, b: str) -> bool:
    """One or two edits apart (a swapped pair counts as one), for references of six characters or more."""
    if a == b or len(b) < 6 or abs(len(a) - len(b)) > 2:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]:
            return True
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= 2


def _near(d: date | None, said_on: date | None) -> bool:
    return d is not None and said_on is not None and abs(d - said_on) <= WAIVER_WINDOW
