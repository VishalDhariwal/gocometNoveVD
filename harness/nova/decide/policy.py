"""The action gate: what to do with each verdict, in each mode (GLOSSARY "Actions" and "The two modes").

    no_finding                      none
    conflict                        escalate (both modes)
    expiry                          internal:alert
    late-fee risk                   internal:payment_reminder
    cashback not due yet            internal:track_credit
    outbound remedy (dispute, claim_credit, request_document):
        supervised                  draft:<remedy>, or human_review at $5,000.00 or more on the invoice / cashback key
        autonomous                  execute:<remedy> only when all five unattended checks pass, else human_review

The five checks are run explicitly and kept with their reasons, so every decision can be audited:
  1 evidence   no missing document; repriced lines dated from tracking or a carrier email; valid citations held
  2 conflict   no conflict on this key, nor an open one on the same contract, lane and charge in the same window
  3 threshold  outbound amounts on the invoice (or cashback key) add up to less than $5,000.00
  4 reversible the action is a dispute, a credit claim or a document request
  5 authority  terms at level 1-2, facts at level 3 or better, the recipient taken from Schedule 1
plus one of our own, which only autonomous mode feels:
  6 review     the case reviewer (nova/review) didn't send the item to a person
After this, nova/review/route.py keeps our own holds inside the escalation budget and builds the human review queue.
Recipients only ever come from Schedule 1: the carrier's billing-disputes contact (else its Key Account Manager) for
disputes and claims, the holder for a document request. Never an address from an email body.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from ..knowledge.terms import Terms
from ..knowledge.view import day
from .common import OUTBOUND, ZERO, Verdict

THRESHOLD = Decimal("5000.00")
TERM_KINDS = {"rate", "fuel_rate", "accessorial", "charge_alias", "fx_rate", "contract_term", "contract",
              "schedule1_person", "waiver", "due_date_extension", "contract_notice", "doc_reference"}
DATED_SOURCES = {"tracking", "status_email"}
FREIGHT_CODES = {"FRT", "FUEL"}


@dataclass
class Gate:
    action: str
    recipient: dict | None = None
    checks: dict[str, tuple[bool, str]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(ok for ok, _ in self.checks.values())


def group_of(v: Verdict) -> str:
    """What the $5,000 threshold sums over: the invoice, or the cashback key."""
    return f"invoice|{v.key['invoice_id']}" if "invoice_id" in v.key else v.finding_key


def recipient_for(terms: Terms, v: Verdict) -> dict | None:
    if v.remedy == "request_document":
        h = v.checks.get("missing_holder") or {}
        return h if h.get("email") or h.get("person") else None
    if not v.contract:
        return None
    for category in ("billing_disputes", "notices"):
        h = terms.holder(v.contract, category)
        if h.get("email"):
            return h
    return None


def decide(terms: Terms, verdicts: list[Verdict], mode: str, cited: dict[str, int] | None = None) -> dict[str, Gate]:
    """finding_key -> Gate. `cited`: how many valid citations each item kept after the quote check."""
    cited = cited or {}
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for v in verdicts:
        if v.verdict == "finding" and v.remedy in OUTBOUND and v.amount_usd is not None:
            totals[group_of(v)] += v.amount_usd
    conflicts = [v for v in verdicts if v.verdict == "conflict"]
    return {v.finding_key: _gate(terms, v, mode, totals, conflicts, cited.get(v.finding_key)) for v in verdicts}


def _gate(terms: Terms, v: Verdict, mode: str, totals: dict, conflicts: list[Verdict], n_cited: int | None) -> Gate:
    if v.verdict == "no_finding":
        return Gate("none")
    if v.verdict == "conflict":
        return Gate("escalate")
    if v.remedy == "alert":
        return Gate("internal:alert")
    if v.remedy == "payment_reminder":
        return Gate("internal:payment_reminder")
    if v.remedy == "track_credit":
        return Gate("internal:track_credit")
    if v.remedy not in OUTBOUND:
        return Gate("none")
    recipient = recipient_for(terms, v)
    total = totals.get(group_of(v), ZERO)
    gate = Gate("", recipient, {
        "evidence": _evidence(v, n_cited),
        "conflict": _conflict(v, conflicts),
        "threshold": (total < THRESHOLD, f"outbound on {group_of(v).split('|')[-1]}: ${total:.2f}"),
        "reversible": (True, f"{v.remedy} can be withdrawn"),
        "authority": _authority(v, recipient),
        "review": _review(v),
    })
    if mode == "supervised":
        gate.action = f"draft:{v.remedy}" if gate.checks["threshold"][0] else "human_review"
    else:
        gate.action = f"execute:{v.remedy}" if gate.passed else "human_review"
    return gate


def _evidence(v: Verdict, n_cited: int | None) -> tuple[bool, str]:
    if n_cited is not None and n_cited == 0:
        return False, "no citation survived the quote check"
    if v.verdict == "cannot_determine":
        if not v.evidence:
            return False, "the document citing the missing one isn't held"
        return True, "the citing document is held"
    sources = v.checks.get("pickup_sources")
    if sources and not set(sources) <= DATED_SOURCES:
        return False, f"pickup date from {', '.join(s for s in sources if s not in DATED_SOURCES)}"
    return True, "every document held; pickup dates from tracking or carrier email" if sources else "every document held"


def _conflict(v: Verdict, conflicts: list[Verdict]) -> tuple[bool, str]:
    if v.verdict == "conflict":
        return False, "this key is in conflict"
    for line in v.checks.get("priced") or []:
        if line.get("code") not in FREIGHT_CODES:
            continue
        on = day(line.get("pickup"))
        for c in conflicts:
            if c.contract != v.contract:
                continue
            for w in c.checks.get("conflict_windows") or []:
                if w.get("lane") != line.get("lane"):
                    continue
                start, end = day(w.get("from")), day(w.get("to"))
                if on is None or ((start is None or start <= on) and (end is None or on <= end)):
                    return False, f"open conflict {c.finding_key} on {w['lane']}"
    return True, "no open conflict on this contract, lane and period"


def _review(v: Verdict) -> tuple[bool, str]:
    r = v.checks.get("review") or {}
    if r.get("person"):
        return False, f"the case reviewer sends it to a person ({str(r['person']).replace('_', ' ')})"
    return True, "the case reviewer didn't send it to a person"


def _authority(v: Verdict, recipient: dict | None) -> tuple[bool, str]:
    if recipient is None:
        return False, "no Schedule 1 recipient"
    if v.remedy == "request_document":   # asking for a document relies on no term, only on whom we ask
        return True, f"holder {recipient.get('person')} from Schedule 1"
    for s in v.evidence:
        limit = 2 if s.fact.kind in TERM_KINDS else 3
        if s.level > limit:
            return False, f"{s.fact.kind} in {s.fact.doc_ref} is only level {s.level}"
    return True, f"terms level 1-2, facts level 3 or better; recipient {recipient.get('person')} from Schedule 1"
