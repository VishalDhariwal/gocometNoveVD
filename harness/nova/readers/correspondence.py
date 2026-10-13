"""Emails (and letters): what does the sender say that changes something?

Intents recorded: waivers, due-date extensions, rate notices, shipment status, payment requests on an invoice,
contract notices (extension, renewal, termination) and customs notices. Queries and cover notes ("please find
attached") produce nothing.

Who said it is kept, never judged here: the sender's address (lower-cased) goes on the fact, and anything taken from
quoted or forwarded text is marked as such. Whether that person had authority is decided at report time, against
Schedule 1 and feedback. Text aimed at automated readers is recorded as an injection attempt and nothing else.
"""
from __future__ import annotations

from .. import config
from .. import normalize as norm
from ..llm import Gateway
from . import values as v
from .base import (AUTOMATION_FIELD, COMMON_RULES, QUOTE, STR, STR_LIST, DocContext, FactOut, ReadOutcome, call,
                   ground, injection_facts, tool)

VERSION = "correspondence-v2"

INTENTS = ["waiver", "due_date_extension", "rate_notice", "status_update", "payment_request", "contract_notice",
           "customs_notice", "query", "cover_note", "other"]

SYSTEM = COMMON_RULES + """

This is an email (or a letter) the shipper's accounts-payable team received or sent. Record each thing it says that could change what is billed or owed, as one intent each:
- waiver: a charge is waived or written off. Copy the charges, waybills (AWBs), invoices and months it names, any condition it sets, and words about its scope ("in full", "this once", "going forward"). condition_kind: none (unconditional), payment_by_date (the waiver holds only if something is paid by a date; put that date in condition_date and the invoice that must be paid in condition_invoice), or other (any other condition; keep its words in condition).
- due_date_extension: an invoice's due date is moved. Copy the invoice and the new due date.
- rate_notice: rates change. Copy the agreement number if stated or evident from the notice reference, the reference, the effective dates, and every rate row (lane, band as printed, band_kind per_kg or minimum_charge, band_from and band_to copied from the band, rate, currency).
- status_update: a shipment was picked up, delivered, collected, held or released. Copy the waybill, the date and the station; put the words used in event_as_printed and choose event from the event list below by what happened.
- payment_request: payment of a specific invoice is requested. Copy the invoice, whether payment is asked for, whether it says the invoice is outstanding, and any marker such as copy, duplicate, reminder or "for your records" (as printed in marker, and as marker_kind: none, copy, duplicate, reminder, for_your_records, reissued, pro_forma or other).
- contract_notice: an agreement is extended, renewed, not renewed or terminated. Copy the agreement, the kind of notice and the dates.
- customs_notice: a shipment was held or examined by customs. Copy the waybill, the dates, the station, the reason code and the reason, and choose cause from the list below.
- query: a question or request for checking, with no decision in it.
- cover_note: only says documents are attached.
Mark in_quoted_text when the words come from quoted or forwarded text rather than the sender's own message, and name who originally wrote them. Each intent's quote must be copied from the email.

""" + v.EVENT_HELP + "\n\n" + v.CAUSE_HELP

INTENT = {"type": "object", "properties": {
    "type": {"enum": INTENTS},
    "in_quoted_text": {"type": "boolean"}, "original_sender": STR, "original_sender_email": STR,
    "charges": STR_LIST, "awbs": STR_LIST, "invoices": STR_LIST, "months": STR_LIST, "condition": STR,
    "condition_kind": {"enum": ["none", "payment_by_date", "other"]}, "condition_date": STR, "condition_invoice": STR,
    "scope_words": STR,
    "invoice": STR, "new_due_date": STR,
    "contract_id": STR, "reference": STR, "effective_from": STR, "effective_to": STR,
    "rates": {"type": "array", "items": {"type": "object", "properties": {
        "lane": STR, "band": STR, "band_kind": {"enum": ["per_kg", "minimum_charge"]}, "band_from": STR, "band_to": STR,
        "rate": STR, "currency": STR, "quote": QUOTE}, "required": ["lane", "band", "rate"]}},
    "awb": STR, "event": {"enum": v.EVENTS}, "event_as_printed": STR, "date": STR, "station": STR,
    "hold_date": STR, "release_date": STR, "reason_code": STR, "reason": STR, "cause": {"enum": v.CAUSES},
    "asks_for_payment": {"type": "boolean"}, "says_outstanding": {"type": "boolean"}, "marker": STR,
    "marker_kind": {"enum": ["none"] + v.COPY_MARKERS},
    "notice_type": {"enum": ["extension", "renewal", "non_renewal", "termination", "other"]}, "new_end_date": STR,
    "quote": QUOTE,
}, "required": ["type", "quote"]}

TOOL = tool("record_email", "Record what an email or letter says that matters to accounts payable.", {
    "intents": {"type": "array", "items": INTENT},
    "automation_instructions": AUTOMATION_FIELD,
}, ["intents", "automation_instructions"])


def _prompt(ctx: DocContext) -> tuple[str, str | None]:
    """-> (prompt, sender address)."""
    if ctx.email:
        e = ctx.email
        head = (f"From: {e.get('from_name') or ''} <{e.get('from_addr')}>\nTo: {', '.join(e.get('to_addrs') or [])}\n"
                f"Date: {ctx.received_at}\nSubject: {e.get('subject')}")
        body = (f"<email>\n{head}\n\n<sender_wrote>\n{e.get('authored_text') or ''}\n</sender_wrote>\n"
                f"<quoted_or_forwarded>\n{e.get('quoted_text') or ''}\n</quoted_or_forwarded>\n</email>")
        return body, e.get("from_addr")
    sender = (ctx.parent_email or {}).get("from_addr")
    return (f"(Context: a letter attached to an email from {sender}.)\n" + ctx.document_block()), sender


class CorrespondenceReader:
    name = "correspondence"
    version = VERSION

    def read(self, ctx: DocContext, gateway: Gateway) -> ReadOutcome:
        out = ReadOutcome(self.name, self.version)
        prompt, sender = _prompt(ctx)
        data = call(gateway, out, purpose="correspondence", models=config.READER_MODELS, system=SYSTEM, tool_def=TOOL,
                    content=prompt, max_tokens=6000)
        out.facts, out.warnings = build(ctx, data, sender)
        kinds = sorted({i.get("type") for i in data.get("intents") or []} - {None})
        out.doc_type = "+".join(kinds) or "email"
        out.status = "partial" if out.warnings else "ok"
        return out


def build(ctx: DocContext, data: dict, sender: str | None) -> tuple[list[FactOut], list[str]]:
    problems: list[str] = []
    facts = injection_facts(ctx, data.get("automation_instructions"))
    intents = data.get("intents") or []
    order = v.date_order([i.get(k) for i in intents for k in ("new_due_date", "effective_from", "effective_to", "date",
                                                               "hold_date", "release_date", "new_end_date")])
    day = lambda raw: v.iso(v.parse_date(raw, order))  # noqa: E731
    sender = norm.email(sender) if sender else None

    for it in intents:
        kind = it.get("type")
        quoted = bool(it.get("in_quoted_text"))
        who = norm.email(it["original_sender_email"]) if quoted and it.get("original_sender_email") else sender
        base = {"sender_email": sender, "from_quoted_text": quoted, "original_sender": it.get("original_sender"),
                "received_at": ctx.received_at}

        def add(fact_kind, payload, check, keys, **extra):
            page, quote, ver = ground(ctx, it.get("quote"), check)
            if ver == "unverified":
                problems.append(f"{kind} isn't in the email as quoted: {str(it.get('quote'))[:80]!r}")
            facts.append(FactOut(fact_kind, {**base, **payload}, keys=keys, page=page, quote=quote, verification=ver,
                                 **extra))

        if kind == "waiver":
            if not who:
                problems.append("a waiver with no sender address")
                continue
            add("waiver", {"charges": it.get("charges") or [],
                           "charge_keys": [norm.label(c) for c in it.get("charges") or []],
                           "awbs": [norm.awb(a) for a in it.get("awbs") or [] if a],
                           "invoices": [norm.invoice_id(x) for x in it.get("invoices") or [] if x],
                           "months": [m for m in (v.parse_month(x) for x in it.get("months") or []) if m],
                           "condition": v.text_or_none(it.get("condition")),
                           "condition_kind": v.choice(it.get("condition_kind"), ["none", "payment_by_date", "other"],
                                                      "other" if v.text_or_none(it.get("condition")) else "none"),
                           "condition_date": day(it.get("condition_date")),
                           "condition_invoice": norm.invoice_id(it["condition_invoice"])
                           if v.text_or_none(it.get("condition_invoice")) else None,
                           "scope_words": v.text_or_none(it.get("scope_words"))},
                (it.get("awbs") or [])[:1], {"person_email": who})
        elif kind == "due_date_extension":
            if not (who and v.text_or_none(it.get("invoice")) and day(it.get("new_due_date"))):
                problems.append(f"an extension missing its invoice or new date: {str(it.get('quote'))[:80]!r}")
                continue
            add("due_date_extension", {"new_due_date": day(it.get("new_due_date")),
                                       "new_due_date_printed": it.get("new_due_date")},
                [it.get("invoice"), it.get("new_due_date")], {"invoice_id": it["invoice"], "person_email": who})
        elif kind == "rate_notice":
            contract = v.text_or_none(it.get("contract_id"))
            if not contract:
                problems.append("a rate notice that doesn't say which agreement it's under")
                continue
            start, end = day(it.get("effective_from")), day(it.get("effective_to"))
            reference = v.text_or_none(it.get("reference"))
            add("instrument", {"type": "rate_notice", "reference": reference, "effective_from": start,
                               "effective_to": end, "signed_by_both": False},
                [], {"contract_id": contract, "person_email": who}, valid_from=start, valid_to=end)
            for row in it.get("rates") or []:
                kind_, lo, hi, issue = v.band_of(row)
                if issue:
                    problems.append(issue)
                page, quote, ver = ground(ctx, row.get("quote"), [row.get("lane"), row.get("rate")])
                if ver == "unverified":
                    problems.append(f"rate row isn't in the email as quoted: {row}")
                facts.append(FactOut("rate", {**base, "kind": kind_, "band_min": lo, "band_max": hi,
                                              "band_text": row.get("band"), "rate": v.parse_amount(row.get("rate")),
                                              "currency": (v.text_or_none(row.get("currency")) or "").upper() or None,
                                              "period_label": None, "instrument": "rate_notice",
                                              "instrument_ref": reference},
                                     keys={"contract_id": contract, "lane": row.get("lane"), "person_email": who},
                                     page=page, quote=quote, verification=ver, valid_from=start, valid_to=end))
        elif kind == "status_update":
            if not (v.text_or_none(it.get("awb")) and day(it.get("date"))):
                problems.append(f"a status update missing its waybill or date: {str(it.get('quote'))[:80]!r}")
                continue
            event = v.choice(it.get("event"), v.EVENTS, "OTHER")
            add("status_update", {"event": event, "event_as_printed": v.text_or_none(it.get("event_as_printed")),
                                  "date": day(it.get("date")), "station": v.text_or_none(it.get("station"))},
                [it.get("awb")], {"awb": it["awb"], "person_email": sender})
        elif kind == "payment_request":
            if not v.text_or_none(it.get("invoice")):
                continue
            add("payment_request", {"asks_for_payment": bool(it.get("asks_for_payment")),
                                    "says_outstanding": bool(it.get("says_outstanding")),
                                    "marker": v.text_or_none(it.get("marker")),
                                    "marker_kind": v.choice(it.get("marker_kind"), ["none"] + v.COPY_MARKERS,
                                                            "other" if v.text_or_none(it.get("marker")) else "none")},
                [], {"invoice_id": it["invoice"], "person_email": sender})
        elif kind == "contract_notice":
            if not v.text_or_none(it.get("contract_id")):
                problems.append("a contract notice that doesn't name the agreement")
                continue
            add("contract_notice", {"notice_type": it.get("notice_type") or "other",
                                    "new_end_date": day(it.get("new_end_date")),
                                    "effective_from": day(it.get("effective_from"))},
                [], {"contract_id": it["contract_id"], "person_email": sender})
        elif kind == "customs_notice":
            if not v.text_or_none(it.get("awb")):
                continue
            add("customs_notice", {"hold_date": day(it.get("hold_date")), "release_date": day(it.get("release_date")),
                                   "station": v.text_or_none(it.get("station")),
                                   "reason_code": v.text_or_none(it.get("reason_code")),
                                   "reason": v.text_or_none(it.get("reason")),
                                   "cause": v.choice(it.get("cause"), v.CAUSES, "not_stated")},
                [it.get("awb")], {"awb": it["awb"], "person_email": sender})
    return facts, problems
