"""What a person decided about one finding key, applied to that key and nothing else (GLOSSARY "feedback.json").

    close              stop raising the key (a new invoice with the same error is a new key, and is raised)
    reject             the key is not a finding: drop it
    override_amount    the key's amount is the one the person gave (fact.value.amount_usd)

Decisions that change *terms* rather than one key (sender_authority, rate_accepted, precedence) work inside the
terms engine and the detectors, inside their scope; a one-off waiver approval works inside the waiver detector, on
its own key only. Each decision counts from the day it was made (the view only loads feedback decided by as_of).
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from ..knowledge.terms import Terms
from ..money import to_cents
from .common import Verdict

log = logging.getLogger(__name__)
KEY_DECISIONS = ("close", "reject", "override_amount")


def apply(terms: Terms, verdicts: list[Verdict]) -> list[Verdict]:
    out = []
    for v in verdicts:
        decided = [it for it in terms.feedback.for_key(v.finding_key) if it.get("decision") in KEY_DECISIONS]
        if not decided:
            out.append(v)
            continue
        it = decided[-1]   # the latest decision about this key wins
        if it["decision"] in ("close", "reject"):
            log.info("%s dropped: feedback %s (%s)", v.finding_key, it["item_id"], it["decision"])
            continue
        try:
            amount = Decimal(str(((it.get("fact") or {}).get("value") or {}).get("amount_usd")))
        except (InvalidOperation, TypeError):
            out.append(v)
            continue
        v.verdict, v.amount_usd, v.amount_range, v.missing_doc = "finding", to_cents(amount), None, None
        if v.remedy not in ("dispute", "claim_credit"):
            v.remedy = "claim_credit" if v.cls == "cashback" else "dispute"
        if (v.currency or "USD").upper() == "USD":
            v.amount_native = v.amount_usd
        v.why = f"amount set by feedback {it['item_id']}"
        v.learn(it["item_id"])
        out.append(v)
    return out
