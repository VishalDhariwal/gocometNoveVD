"""Code's side: what a reviewer's opinion is worth. Nothing it says counts until it passes these tests.

Every citation is checked like the grader checks one (the quote on the cited page) and given the authority level of
its document (library.Library.level). Then, by the kind of case:

hold (second_look)   the reviewer disagrees and at least one checked quote at level 3 or better backs it -> the item
                     is held for a person (human_review in autonomous). Code's verdict and amount stay as they are.
upgrade              only two findings can come from the reviewer, each with a mechanical test:
  waived_but_billed  the waiver email is held, its sender is the Schedule 1 waivers contact for that contract (or a
                     feedback grant), a checked quote from it is in the sender's own words; its condition is none, or
                     met with a checked quote at level 3 or better from another document; the email names each
                     line's waybill, invoice or month (letters and digits compared); the lines are on this invoice and
                     nothing else has claimed them.
  duplicate          each repeated line and the earlier one it repeats: two invoice numbers, the same waybill, the same
                     amount, the earlier invoice dated no later; no credit note touches either invoice; unclaimed.
                     The amount is the repeated lines, added up by code.
Anything else -> no change. Any opinion resting on level-5 text (quoted, outsiders, text aimed at automated readers)
is refused. What comes out carries checks["review"], which keeps it from being executed (decide/policy.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .. import normalize as norm
from ..decide.common import ZERO, Verdict, amount_of, invoice_date, lines_of, to_usd
from ..knowledge.terms import Source
from ..knowledge.view import day
from ..store import Fact
from .library import INJECTED, Checked, Library, compact
from .triage import Case

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
_ADDRESS = re.compile(r"\S+@\S+|https?://\S+|www\.\S+", re.I)
_PHONE = re.compile(r"\+?\d[\d ()\-]{8,}\d")


@dataclass
class Outcome:
    kind: str                      # upgraded | held | agreed | no_change | rejected
    detail: str
    verdict: Verdict | None = None


def clean(text: str, n: int = 300) -> str:
    """The reviewer's words as they may be printed: no addresses, links or phone numbers (they could be planted)."""
    return _PHONE.sub("[number]", _ADDRESS.sub("[address]", " ".join(str(text).split())))[:n]


def judge(lib: Library, case: Case, opinion: dict, verdicts: dict[str, Verdict], claimed: dict[int, str]) -> Outcome:
    category = {"waived_but_billed": "waivers", "late_fee_not_owed": "payment_terms"}.get(
        opinion.get("finding_class") if case.cls is None else case.cls)
    cites = [c for c in (lib.check(c, case.contract, category) for c in opinion.get("citations") or []) if c]
    n_given = len(opinion.get("citations") or [])
    planted = [c for c in cites if c.why == INJECTED]
    if planted:   # it leaned on text written to steer automated readers: nothing it says is taken
        return Outcome("rejected", f"cites {planted[0].doc} p{planted[0].page}: {INJECTED}")
    cites = [c for c in cites if c.level < 5]   # quoted text and outsiders back nothing
    note = {"reasons": [clean(r) for r in (opinion.get("reasons") or [])[:6]], "reason": case.reason,
            "instructions_seen": bool(opinion.get("instructions_in_documents"))}
    if case.power == "hold":
        return _hold(case, opinion, cites, n_given, note)
    if opinion.get("verdict") != "finding":
        return Outcome("no_change", f"reviewer says {opinion.get('verdict')} ({opinion.get('opinion')})")
    cls = opinion.get("finding_class")
    if case.cls and cls != case.cls:
        return Outcome("no_change", f"reviewer suggests {cls}, but this case is about {case.cls}")
    if cls == "waived_but_billed":
        return _waiver(lib, case, opinion, cites, verdicts, claimed, note)
    if cls == "duplicate":
        return _duplicate(lib, case, opinion, cites, verdicts, claimed, note)
    return Outcome("no_change", f"reviewer suggests {cls}, which only code can find")


# ---- hold: a second look at an execute ---------------------------------------------------------------------------
def _hold(case: Case, opinion: dict, cites: list[Checked], n_given: int, note: dict) -> Outcome:
    v = case.verdict
    if opinion.get("opinion") != "disagree":
        if opinion.get("opinion") == "agree":
            v.checks["review"] = {**note, "agreed": True}
            return Outcome("agreed", "the reviewer agrees", v)
        return Outcome("no_change", "the reviewer is unsure")
    backing = [c for c in cites if c.verified and c.level <= 3]
    if not backing:
        return Outcome("no_change", f"disagrees, but none of its {n_given} quote(s) checked out at level 3 or better")
    v.checks["review"] = {**note, "held": True,
                          "citations": [{"doc": c.doc, "page": c.page, "quote": c.quote} for c in backing[:3]]}
    return Outcome("held", f"disagrees, backed by {backing[0].doc} p{backing[0].page} ({backing[0].why})", v)


# ---- upgrade: a waiver code couldn't judge -----------------------------------------------------------------------
def _waiver(lib: Library, case: Case, opinion: dict, cites: list[Checked], verdicts: dict, claimed: dict,
            note: dict) -> Outcome:
    terms, view = lib.terms, lib.view
    inv, contract = case.invoice_id, case.contract
    header = lib.headers.get(inv or "")
    if header is None or not contract:
        return Outcome("rejected", "no invoice or contract to put it on")
    target = norm.finding_key({"class": "waived_but_billed", "invoice_id": inv})
    if (old := verdicts.get(target)) is not None and old.verdict == "finding":
        return Outcome("rejected", f"{target} is already a finding")
    doc = lib.by_ref.get(str(opinion.get("waiver_doc") or "").strip())
    if doc is None or doc.doc_id not in lib.emails:
        return Outcome("rejected", "the waiver email it names isn't held")
    person = terms.auth.schedule1(contract, doc.from_addr, "waivers")
    grant = None
    if person is None and doc.from_name:
        grant = terms.feedback.grant(contract=contract, person=doc.from_name, category="waivers",
                                     on=view.as_of_date)
    if person is None and grant is None:
        return Outcome("rejected", f"the waiver's sender isn't on Schedule 1 for waivers on {contract}")
    own = [c for c in cites if c.doc_id == doc.doc_id and c.verified and c.level == 2]
    if not own:
        return Outcome("rejected", "no checked quote of the waiver in its author's own words")
    cond = opinion.get("condition")
    others = [c for c in cites if c.doc_id != doc.doc_id and c.verified and c.level <= 3]
    if cond not in ("none", "met"):
        return Outcome("no_change", f"the waiver's condition is {cond}")
    if cond == "met" and not others:
        return Outcome("rejected", "says the condition was met, with no checked record (level 1-3) showing it")
    if case.reason == "waiver_condition" and cond == "none":
        return Outcome("rejected", "code read a condition in this waiver; the reviewer says there is none")
    text = compact(lib.emails[doc.doc_id]["own"])
    words = " ".join(lib.emails[doc.doc_id]["own"].lower().split())
    own_lines = {f.id: f for f in lines_of(view, header)}
    kept, dropped = [], []
    for i in dict.fromkeys(opinion.get("lines") or []):
        f = own_lines.get(i)
        if f is None:
            dropped.append(f"{i}: not a line of {inv}")
        elif claimed.get(i):
            dropped.append(f"{i}: already claimed as {claimed[i]}")
        elif not _named(f, header, text, words):
            dropped.append(f"{i}: the email doesn't name its waybill, invoice or month")
        else:
            kept.append(f)
    if not kept:
        return Outcome("rejected", "no line passed: " + "; ".join(dropped or ["none given"]))
    native = sum((amount_of(f) for f in kept), ZERO)
    v = Verdict("waived_but_billed", {"invoice_id": inv}, "finding", contract=contract,
                currency=header.payload.get("currency"))
    usd, fx = to_usd(terms, contract, v.currency, native, invoice_date(header))
    if usd is None:
        return Outcome("rejected", "the amount can't be converted to USD")
    who = (person.payload.get("name") if person else doc.from_name) or doc.from_addr
    v.remedy, v.amount_native, v.amount_usd, v.lines = "dispute", native, usd, [f.id for f in kept]
    v.why = "billed a charge the carrier waived (found by the case reviewer, checked by code)"
    for f in kept:
        v.steps.append(f"{f.payload.get('label')} on {f.awb or f.payload.get('period') or inv}: {amount_of(f)} billed; "
                       f"waived by {who} in {doc.doc_ref} ({doc.received_at[:10]}), Schedule 1 waivers contact"
                       + (f", confirmed by feedback {grant['item_id']}" if grant else ""))
        v.cite(Source(f, 3, "the billed line"))
    v.steps.append(f"Waived lines: {' + '.join(str(amount_of(f)) for f in kept)} = {native}")
    if cond == "met":
        v.steps.append(f"The waiver's condition was met: {others[0].doc} p{others[0].page}")
    v.cite(*[Source(_fact(lib, c, "waiver", contract), 2, clean(c.proves or "the waiver", 120)) for c in own[:2]])
    v.cite(Source(person, 1, "Schedule 1 waivers contact") if person else None)
    v.cite(*[Source(_fact(lib, c, "review_citation", contract), c.level, clean(c.proves or "the condition", 120))
             for c in others[:2]])
    if grant:
        v.learn(grant["item_id"])
    v.checks.update(fx=fx.sources[0] if fx is not None else None, review={**note, "upgraded": True})
    detail = f"waived_but_billed {usd} on {len(kept)} line(s)" + (f"; dropped {'; '.join(dropped)}" if dropped else "")
    return Outcome("upgraded", detail, v)


def _named(f: Fact, header: Fact, text: str, words: str) -> bool:
    if f.awb and len(compact(f.awb)) >= 5 and compact(f.awb) in text:
        return True
    if compact(header.invoice_id) and compact(header.invoice_id) in text:
        return True
    d: date | None = day(f.payload.get("pickup_date")) or invoice_date(header)
    period = str(f.payload.get("period") or "")
    if len(period) == 7 and period[4] == "-":
        d = date(int(period[:4]), int(period[5:]), 1)
    if d is None:
        return False
    month = MONTHS[d.month - 1]
    return (f"{month} {d.year}" in words or f"{month[:3]} {d.year}" in words or f"{d.year}-{d.month:02d}" in words
            or f"{month[:3]}-{d.year}" in words or f"{month[:3]} {str(d.year)[2:]}" in words)


# ---- upgrade: a charge billed twice ------------------------------------------------------------------------------
def _duplicate(lib: Library, case: Case, opinion: dict, cites: list[Checked], verdicts: dict, claimed: dict,
               note: dict) -> Outcome:
    terms, view = lib.terms, lib.view
    inv, contract = case.invoice_id, case.contract
    header = lib.headers.get(inv or "")
    if header is None:
        return Outcome("rejected", "no invoice to put it on")
    target = norm.finding_key({"class": "duplicate", "invoice_id": inv})
    if (old := verdicts.get(target)) is not None and old.verdict == "finding":
        return Outcome("rejected", f"{target} is already a finding")
    own_lines = {f.id: f for f in lines_of(view, header)}
    credited = {c.invoice_id for c in view.kind("credit_note")} | {
        i for c in view.kind("credit_note") for i in (c.payload.get("refers_to_invoices") or [])}
    kept, dropped = [], []
    for r in opinion.get("repeats") or []:
        f, e = own_lines.get(r.get("line")), lib.facts_by_id.get(r.get("earlier_line"))
        why = _repeat_problem(lib, f, e, header, claimed, credited)
        if why:
            dropped.append(f"{r.get('line')}: {why}")
        elif all(f.id != k.id for k, _ in kept):
            kept.append((f, e))
    if not kept:
        return Outcome("rejected", "no repeated line passed: " + "; ".join(dropped or ["none given"]))
    native = sum((amount_of(f) for f, _ in kept), ZERO)
    v = Verdict("duplicate", {"invoice_id": inv}, "finding", contract=contract, currency=header.payload.get("currency"))
    usd, fx = to_usd(terms, contract, v.currency, native, invoice_date(header))
    if usd is None:
        return Outcome("rejected", "the amount can't be converted to USD")
    v.remedy, v.amount_native, v.amount_usd, v.lines = "dispute", native, usd, [f.id for f, _ in kept]
    v.why = "the same charge was billed on an earlier invoice (found by the case reviewer, checked by code)"
    for f, e in kept:
        v.steps.append(f"'{f.payload.get('label')}' on {f.awb}: {amount_of(f)}, already billed on {e.invoice_id} as "
                       f"'{e.payload.get('label')}' ({amount_of(e)}): same waybill and amount")
        v.cite(Source(f, 3, "billed again"), Source(e, 3, f"first billed on {e.invoice_id}"))
    v.steps.append(f"Billed twice: {' + '.join(str(amount_of(f)) for f, _ in kept)} = {native}")
    v.cite(*[Source(_fact(lib, c, "review_citation", contract), c.level, clean(c.proves or "the reviewer's reading", 120))
             for c in cites if c.verified and c.level <= 3][:2])
    v.checks.update(fx=fx.sources[0] if fx is not None else None, review={**note, "upgraded": True})
    detail = f"duplicate {usd} on {len(kept)} line(s)" + (f"; dropped {'; '.join(dropped)}" if dropped else "")
    return Outcome("upgraded", detail, v)


def _repeat_problem(lib: Library, f: Fact | None, e: Fact | None, header: Fact, claimed: dict,
                    credited: set) -> str | None:
    if f is None:
        return "not a line of this invoice"
    if e is None or e.kind != "invoice_line":
        return "the earlier line isn't a billed line we hold"
    if e.invoice_id == f.invoice_id:
        return "both lines are on the same invoice number"
    if claimed.get(f.id):
        return f"already claimed as {claimed[f.id]}"
    if not f.awb or norm.awb(f.awb) != norm.awb(e.awb or ""):
        return "different waybills"
    if amount_of(f) != amount_of(e) or amount_of(f) <= 0:
        return "different amounts"
    earlier = lib.headers.get(e.invoice_id)
    if earlier is None:
        return "the earlier invoice isn't held"
    a, b = invoice_date(earlier), invoice_date(header)
    if a and b and a > b:
        return "the other invoice is the later one"
    if (a, earlier.known_at) == (b, header.known_at) and earlier.id > header.id:
        return "the other invoice is the later one"
    if header.invoice_id in credited or e.invoice_id in credited:
        return "a credit note touches one of the invoices; left to a person"
    return None


_SYNTHETIC = iter(range(-1, -10**9, -1))


def _fact(lib: Library, c: Checked, kind: str, contract: str | None) -> Fact:
    """A quote the reviewer found, shaped like a fact so the report prints and re-checks it like any other."""
    return Fact(next(_SYNTHETIC), kind, {"from_reviewer": True}, contract, None, None, None, None, None, None, None,
                None, lib.docs[c.doc_id].received_at, c.doc_id, c.doc, c.page, c.quote,
                "verified" if c.verified else "unverified")
