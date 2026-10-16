"""Plain-language explanation for every report item, and what each piece of evidence is there for.

`explanation` is a list of sentences: first the conclusion, then the working, in the order the code actually did it
(the billed line, the term that applies and why it applies, the record that dates or confirms it, the arithmetic).
A person can check every number against the cited quotes without redoing the audit.

Each evidence entry gets a `role` (what the document proves: the billed line, the contract rate, the pickup date,
the person's authority...) and a `detail` (why this one was used). Both are extra fields the report schema allows.
"""
from __future__ import annotations

from ..decide.common import Verdict
from ..knowledge.terms import Source

MAX_STEPS = 20

RATE_ROLES = {"msa": "contract rate (Annex A)", "amendment": "amendment rate", "annex_update": "annex update rate",
              "rate_notice": "rate notice"}
ROLES = {
    "invoice_line": "billed line", "invoice": "the invoice", "credit_note": "credit note",
    "fuel_rate": "fuel surcharge (Annex C)", "accessorial": "contract fee (Annex D)",
    "charge_alias": "charge codes (Annex E)", "fx_rate": "exchange rate (Annex F)",
    "contract_term": "contract clause", "contract": "contract term", "instrument": "contract document",
    "schedule1_person": "authority (Schedule 1)", "doc_reference": "where the missing document is cited",
    "tracking_event": "tracking record", "status_update": "carrier status email", "shipment": "air waybill",
    "dg_declaration": "dangerous goods declaration", "customs_notice": "customs notice",
    "ledger_entry": "payment register", "waiver": "waiver", "due_date_extension": "due date extension",
    "payment_request": "request for payment", "contract_notice": "extension notice",
}


def role(src: Source) -> str:
    kind = src.fact.kind
    if kind == "rate":
        return RATE_ROLES.get(src.fact.payload.get("instrument"), "rate")
    if kind == "tracking_event" and src.fact.payload.get("event") == "PICKED_UP":
        return "pickup date (tracking)"
    return ROLES.get(kind, kind.replace("_", " "))


def _usd(v: Verdict) -> str:
    if v.amount_usd is None:
        return ""
    native = ""
    if v.currency and v.currency.upper() != "USD" and v.amount_native is not None:
        native = f" ({v.currency} {v.amount_native:.2f})"
    return f"USD {v.amount_usd:.2f}{native}"


def headline(v: Verdict) -> str:
    key = v.key.get("invoice_id") or v.key.get("contract_id")
    if v.verdict == "conflict" and v.amount_range:
        lo, hi = v.amount_range
        return (f"Invoice {key}: two sources with authority disagree, and the contract doesn't rank them, so the "
                f"overcharge is between USD {lo:.2f} and USD {hi:.2f} depending on which applies ({v.why}). "
                f"A person has to decide.")
    if v.verdict == "cannot_determine" and v.missing_doc:
        h = v.missing_doc.get("holder") or {}
        who = ", ".join(x for x in (h.get("person"), h.get("role"), h.get("party")) if x)
        return (f"{key}: can't be checked without '{v.missing_doc['name']}', which we don't hold ({v.why}). "
                f"Ask {who or 'the issuing party'} for it.")
    if v.verdict == "no_finding":
        return f"{key}: nothing to recover ({v.why})."
    if v.cls == "overcharge":
        return f"Invoice {key} was billed {_usd(v)} above the contract price."
    if v.cls == "duplicate":
        return f"Invoice {key} bills {_usd(v)} that was already billed ({v.why})."
    if v.cls == "waived_but_billed":
        return f"Invoice {key} bills {_usd(v)} of charges the carrier waived in writing."
    if v.cls == "late_fee_not_owed":
        return f"Invoice {key} bills {_usd(v)} of late fees that weren't owed."
    if v.cls == "late_fee_risk":
        return (f"Invoice {key} is unpaid and due on {v.deadline}; paying late would cost about {_usd(v)} a month in "
                f"late fees.")
    if v.cls == "cashback":
        kind = str(v.key.get("credit_type", "")).replace("_", " ")
        due = "it is due now: claim it" if v.remedy == "claim_credit" else f"not due until {v.deadline}: track it"
        return f"{key} owes a {kind} credit of {_usd(v)} for {v.key.get('period')}, not yet credited; {due}."
    if v.cls == "expiry":
        return f"Contract {key}: {v.why}. Act by {v.deadline}."
    return v.why


def explanation(v: Verdict) -> list[str]:
    out, seen = [headline(v)], set()
    for step in v.steps:
        s = " ".join(str(step).split())
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) > MAX_STEPS:
            out.append("(more working in the audit trail)")
            break
    return out
