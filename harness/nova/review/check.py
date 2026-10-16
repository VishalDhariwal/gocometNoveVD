"""What code does with the reviewer's decision: a few seat belts, then the decision becomes a verdict.

The reviewer decides. Code doesn't second-guess its reasoning; it only keeps the things a model can't be trusted with
by construction, each of which costs points or a hard gate when it goes wrong, and none of which costs a token:

  1 quotes      every citation must be on the page it names (the grader checks evidence this way); a finding needs at
                least one that is
  2 planted     a decision citing text recorded as aimed at automated readers is refused whole (following an
                injection caps the score); quoted text and outsiders back nothing
  3 money       a finding's amount is the finding_amount calculation it names, never a number it wrote
  4 one claim   a billed line already claimed by another finding isn't claimed twice
  5 no drop     a finding code made is never turned into no_finding by the reviewer: it goes to a person instead
                (an injected "this is fine" must not erase a real finding)
Recipients (Schedule 1 only) and "never execute in supervised mode" are the action gate's (decide/policy.py), as for
every item. When the reviewer sends a case to a person, the verdict carries it in checks["review"]["person"]: the gate
then holds it in autonomous mode, and route.py puts it in the human review queue.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import normalize as norm
from ..decide.common import Verdict
from ..knowledge.terms import Source
from ..store import Fact
from .library import INJECTED, Checked, Library
from .tools import Session
from .triage import Case

CLASSES = ("overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed")
CATEGORY = {"waived_but_billed": "waivers", "late_fee_not_owed": "payment_terms"}
_ADDRESS = re.compile(r"\S+@\S+|https?://\S+|www\.\S+", re.I)
_PHONE = re.compile(r"\+?\d[\d ()\-]{8,}\d")


@dataclass
class Outcome:
    kind: str                      # reported | confirmed | no_change | sent_to_person | rejected | failed
    detail: str
    verdict: Verdict | None = None
    person: dict | None = None     # the reviewer sends it to a person: {reason, note, citations}


def clean(text: str, n: int = 300) -> str:
    """The reviewer's words as they may be printed: no addresses, links or phone numbers (they could be planted)."""
    return _PHONE.sub("[number]", _ADDRESS.sub("[address]", " ".join(str(text).split())))[:n]


def judge(lib: Library, case: Case, d: dict, session: Session, verdicts: dict[str, Verdict],
          claimed: dict[int, str]) -> Outcome:
    cls = d.get("finding_class") if d.get("finding_class") in CLASSES else case.cls
    contract = case.contract
    checked = [lib.check(c, contract, CATEGORY.get(cls)) for c in d.get("citations") or []]
    planted = [c for c in checked if c is not None and c.why == INJECTED]
    if planted:                                                                  # seat belt 2
        return Outcome("rejected", f"cites {planted[0].doc} p{planted[0].page}: {INJECTED}")
    cites = [c for c in checked if c is not None and c.level < 5]               # seat belts 1, 2
    note = {"by_reviewer": True, "reason": case.reason,
            "reasons": [clean(x) for x in (d.get("explanation") or [])[:8]],
            "instructions_seen": bool(d.get("instructions_in_documents"))}
    person = None
    if d.get("decision") == "send_to_person":
        person = {"reason": d.get("person_reason") or "outside_the_rules",
                  "note": clean(d.get("note_for_person") or "; ".join(d.get("explanation") or []), 500),
                  "citations": [{"doc": c.doc, "page": c.page, "quote": c.quote[:200]} for c in cites[:3]]}
        note["person"] = person["reason"]
    code = case.verdict
    verdict = d.get("verdict")

    if case.code_finding and verdict != "finding":                              # seat belt 5
        person = person or {"reason": "disagrees_with_code", "note": "; ".join(note["reasons"])[:500],
                            "citations": [{"doc": c.doc, "page": c.page, "quote": c.quote[:200]} for c in cites[:3]]}
        code.checks["review"] = {**note, "person": person["reason"]}
        return Outcome("sent_to_person", f"the reviewer says {verdict}; code's finding stays, for a person", code,
                       person)

    if verdict == "finding":
        if case.code_finding and cls == code.cls and not d.get("amount_calc"):
            code.checks["review"] = {**note, "agreed": True}
            return Outcome("sent_to_person" if person else "confirmed", "the reviewer agrees with code", code, person)
        return _finding(lib, case, d, session, verdicts, claimed, cls, cites, note, person)
    if verdict == "cannot_determine":
        return _cannot_determine(lib, case, d, cls, cites, note, person)
    if person:
        return Outcome("sent_to_person", f"no finding, for a person: {person['reason']}", None, person)
    return Outcome("confirmed" if code is not None else "no_change", "the reviewer finds nothing to report")


def _finding(lib, case, d, session, verdicts, claimed, cls, cites, note, person) -> Outcome:
    calc = session.calcs.get(str(d.get("amount_calc") or "").strip())
    if calc is None or calc.kind != "finding_amount":                           # seat belt 3
        return Outcome("rejected", "the amount isn't a finding_amount calculation")
    inv = calc.invoice_id
    header = lib.headers.get(inv)
    if cls not in CLASSES or header is None:
        return Outcome("rejected", f"no class or invoice to put a finding on ({cls}, {inv})")
    taken = [i for i in calc.lines if claimed.get(i) and claimed[i] != cls]
    if taken:                                                                   # seat belt 4
        return Outcome("rejected", f"line {taken[0]} is already claimed as {claimed[taken[0]]}")
    if not [c for c in cites if c.verified] and not calc.sources:               # seat belt 1
        return Outcome("rejected", "no quote that checks out")
    contract = lib.contract.get(inv) or case.contract
    v = Verdict(cls, {"invoice_id": inv}, "finding", contract=contract, currency=header.payload.get("currency"))
    v.remedy, v.amount_usd, v.amount_native, v.lines = "dispute", calc.value, calc.native, list(calc.lines)
    v.why = {"overcharge": "billed above the contract price", "duplicate": "the same charge was billed before",
             "waived_but_billed": "billed a charge the carrier waived",
             "late_fee_not_owed": "billed a late fee that wasn't owed"}[cls] + " (decided by the case reviewer)"
    v.steps = list(calc.working)
    for line_id in calc.lines:
        f = lib.facts_by_id.get(line_id)
        v.cite(Source(f, 3, "the billed line") if f is not None else None)
    v.cite(*[s for s in calc.sources if isinstance(s, Source)])
    for c in [*calc.checked, *cites][:8]:
        v.cite(Source(_fact(lib, c, "review_citation", contract), c.level, clean(c.proves or "quoted by the reviewer",
                                                                                 120)))
    pickup = _pickup_sources(lib, inv, calc)
    if pickup:
        v.checks["pickup_sources"] = pickup
    v.checks["review"] = note
    old = verdicts.get(v.finding_key)
    detail = f"{cls} USD {v.amount_usd} on {len(v.lines)} line(s)" + (
        f", replacing code's {old.verdict}" if old is not None else "")
    return Outcome("reported", detail, v, person)


def _cannot_determine(lib, case, d, cls, cites, note, person) -> Outcome:
    md = d.get("missing_document") or {}
    inv = norm.invoice_id(d.get("invoice_id") or case.invoice_id or "")
    if cls not in CLASSES or inv not in lib.headers or not md.get("name"):
        return Outcome("rejected", "cannot_determine needs a class, one of our invoices and the document's name")
    if not [c for c in cites if c.verified]:
        return Outcome("rejected", "cannot_determine needs a checked quote of the document that cites the missing one")
    contract = lib.contract.get(inv) or case.contract
    holder = lib.terms.holder(contract, md.get("holder") or "notices") if contract else {}
    v = Verdict(cls, {"invoice_id": inv}, "cannot_determine", contract=contract, remedy="request_document",
                missing_doc={"name": str(md["name"])[:200],
                             "holder": {k: holder[k] for k in ("party", "person", "role") if holder.get(k)}},
                why="the answer depends on a document we don't hold (decided by the case reviewer)")
    v.cite(*[Source(_fact(lib, c, "doc_reference", contract), c.level, clean(c.proves or "cites the document", 120))
             for c in cites if c.verified][:3])
    v.checks.update(missing_holder=holder, review=note)
    return Outcome("reported", f"cannot_determine: {md['name']}", v, person)


def _pickup_sources(lib: Library, inv: str, calc) -> list[str]:
    """Where the pickup dates behind a repriced finding came from, for the unattended-action test."""
    if calc.pickup_from:
        return sorted(set(calc.pickup_from))
    ip = lib.pricing(inv)
    if ip is None:
        return []
    out = set()
    for sp in ip.shipments:
        if any(lp.line.id in calc.lines and lp.code in ("FRT", "FUEL") for lp in sp.lines):
            out.add(sp.pickup.source or "none")
    return sorted(out)


_SYNTHETIC = iter(range(-1, -10**9, -1))


def _fact(lib: Library, c: Checked, kind: str, contract: str | None) -> Fact:
    """A quote the reviewer gave, shaped like a fact so the report prints and re-checks it like any other."""
    return Fact(next(_SYNTHETIC), kind, {"from_reviewer": True}, contract, None, None, None, None, None, None, None,
                None, lib.docs[c.doc_id].received_at, c.doc_id, c.doc, c.page, c.quote,
                "verified" if c.verified else "unverified")
