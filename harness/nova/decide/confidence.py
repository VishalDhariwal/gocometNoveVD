"""How likely an item's verdict and amount are both right, as a number from 0.05 to 0.98.

A logistic score over what we know about the evidence behind it. The weights are set by hand for now and get
calibrated on our own eval set and the practice run (phase 7); the grader scores this with Brier and AUROC, so what
matters is that weaker items score lower, not the exact numbers.
"""
from __future__ import annotations

import math

from ..knowledge.terms import Terms
from .common import Verdict

BASE = {"overcharge": 2.4, "duplicate": 2.3, "waived_but_billed": 2.1, "late_fee_not_owed": 2.1,
        "late_fee_risk": 2.2, "cashback": 1.7, "expiry": 2.8}
VERDICT = {"finding": 0.0, "conflict": -0.3, "cannot_determine": -0.4}
PICKUP = {"tracking": 0.0, "status_email": -0.1, "awb": -0.5, "invoice": -0.9, "none": -1.5}
VERIFICATION = {"verified": 0.0, "structured": 0.0, "repaired": -0.2, "vision": -0.4, "unverified": -1.0}


def score(terms: Terms, v: Verdict, dropped: int = 0, kept: int | None = None) -> float:
    """`dropped`: citations removed by the quote check; `kept`: citations left."""
    logit = BASE.get(v.cls, 2.0) + VERDICT.get(v.verdict, 0.0)
    sources = v.checks.get("pickup_sources") or []
    if sources:
        logit += min(PICKUP.get(s, -1.0) for s in sources)
    if v.evidence:
        logit += min(VERIFICATION.get(s.fact.verification or "verified", -0.5) for s in v.evidence)
    if dropped:
        logit -= 0.6
    if kept == 0:
        logit -= 1.5
    if _label_mapped_by_llm(terms, v):
        logit -= 0.5
    if any("the waybill says" in step for step in v.steps):
        logit -= 0.5                     # printed weight and waybill weight disagree
    if any("outside Annex E left out" in step for step in v.steps):
        logit -= 0.6                     # spend may be missing lines we couldn't classify
    gap = v.checks.get("tier_gap")
    if gap is not None and gap < 0.02:
        logit -= 0.5                     # spend within 2% of a rebate tier: one misread line flips it
    if v.feedback:
        logit += 0.2                     # a person confirmed part of the reasoning
    review = v.checks.get("review") or {}
    if review.get("upgraded"):
        logit -= 0.8                     # rests on the case reviewer's reading, though code checked it
    if review.get("held"):
        logit -= 1.2                     # a second reader disagrees, with a checked quote
    if review.get("agreed"):
        logit += 0.2                     # a second reader went through the documents and agrees
    return round(min(0.98, max(0.05, 1 / (1 + math.exp(-logit)))), 2)


def _label_mapped_by_llm(terms: Terms, v: Verdict) -> bool:
    """A billed line in the evidence whose label isn't in Annex E word for word (the reconciler mapped it)."""
    for s in v.evidence:
        if s.fact.kind != "invoice_line" or not v.contract:
            continue
        key = s.fact.payload.get("label_key")
        if key and not any(key in (a.payload.get("label_keys") or [])
                           for a in terms.view.get("charge_alias", contract_id=v.contract)):
            return True
    return False
