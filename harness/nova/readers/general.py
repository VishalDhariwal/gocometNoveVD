"""The general reader: for any document we don't have a specialist for, so no document is ever skipped.

A proof of delivery, a remittance advice, a dispute reply, a surcharge sheet in a new layout: the classifier calls
these "other", and they used to be skipped. Code can only decide on what it has rules for, so an unknown document
can't produce a finding by itself. What it can do:

- fill slots the engine already has: a shipment event (picked up, delivered, held, released) with its waybill and
  date becomes a status_update fact, exactly as one from a carrier's status email;
- keep everything else that could change what is billed or owed as `document_note` facts (a waiver, an extension, a
  payment, a dispute, a charge, a rate change ...), each with its quote and the invoice, waybill or agreement it names,
  for the case reviewer and for a person to see;
- record the identifiers it names and a document card (summary).

General facts are lower trust: nothing here claims money on its own. Only quotes found on the page are kept.
"""
from __future__ import annotations

from .. import config
from ..llm import Gateway
from . import values as v
from .base import (AUTOMATION_FIELD, COMMON_RULES, QUOTE, STR, STR_LIST, SUMMARY_FIELD, SUMMARY_RULE, DocContext,
                   FactOut, ReadOutcome, call, ground, injection_facts, tool)

VERSION = "general-v1"

NOTE_KINDS = ["waiver", "due_date_extension", "rate_change", "payment", "dispute", "credit", "charge", "instruction",
              "other"]

SYSTEM = COMMON_RULES + """

This document is one the accounts-payable team has no standard form for. Read it for anything that matters to auditing freight bills against contracts:
- seems_to_be: what kind of document it is, in a few words ("proof of delivery", "remittance advice", "dispute reply");
- identifiers: every invoice number, air waybill (AWB) number and agreement number it names, as printed;
- events: each shipment event it records: a shipment picked up, delivered, collected, held or released, with its waybill, date and station; put the words used in event_as_printed and choose event from the event list below by what happened;
- items: each statement that could change what is billed, owed or paid: a charge waived, a due date moved, a rate changed, a payment made or confirmed, an amount disputed, a credit given, a charge billed, an instruction about payment or bank details, or anything else of that sort. For each, the kind, a short description in your words, the invoice, waybill and agreement it concerns, any amount, currency and date, and a quote.
Leave out greetings, addresses, signatures and boilerplate.
""" + SUMMARY_RULE + "\n\n" + v.EVENT_HELP

TOOL = tool("record_document_contents", "Record what a document of no standard form says that matters.", {
    "seems_to_be": STR,
    "identifiers": {"type": "object", "properties": {"invoices": STR_LIST, "awbs": STR_LIST, "agreements": STR_LIST}},
    "events": {"type": "array", "items": {"type": "object", "properties": {
        "awb": STR, "event": {"enum": v.EVENTS}, "event_as_printed": STR, "date": STR, "station": STR, "quote": QUOTE},
        "required": ["awb", "date", "quote"]}},
    "items": {"type": "array", "items": {"type": "object", "properties": {
        "kind": {"enum": NOTE_KINDS}, "description": STR, "invoice": STR, "awb": STR, "agreement": STR,
        "amount": STR, "currency": STR, "date": STR, "quote": QUOTE}, "required": ["kind", "description", "quote"]}},
    "automation_instructions": AUTOMATION_FIELD,
    "summary": SUMMARY_FIELD,
}, ["summary", "events", "items", "automation_instructions"])


def _prompt(ctx: DocContext) -> str:
    sender = (ctx.parent_email or {}).get("from_addr")
    head = (f"(Note from the audit system, not part of the document: it came attached to an email from {sender} with "
            f"subject {(ctx.parent_email or {}).get('subject')!r}.)\n") if ctx.parent_email else ""
    return head + ctx.document_block()


def build(ctx: DocContext, data: dict) -> tuple[list[FactOut], list[str]]:
    problems: list[str] = []
    facts = injection_facts(ctx, data.get("automation_instructions"))
    sender = (ctx.parent_email or ctx.email or {}).get("from_addr")
    seems = v.text_or_none(data.get("seems_to_be"))
    for ev in data.get("events") or []:
        awb, when = v.text_or_none(ev.get("awb")), v.iso(v.parse_date(ev.get("date")))
        if not (awb and when):
            continue
        page, quote, ver = ground(ctx, ev.get("quote"), [awb])
        if ver == "unverified":      # lower trust: an event only counts when its words are on the page
            problems.append(f"an event isn't on the page as quoted: {str(ev.get('quote'))[:80]!r}")
            continue
        keys = {"awb": awb} | ({"person_email": sender} if sender else {})
        facts.append(FactOut("status_update", {"event": v.choice(ev.get("event"), v.EVENTS, "OTHER"),
                                               "event_as_printed": v.text_or_none(ev.get("event_as_printed")),
                                               "date": when, "station": v.text_or_none(ev.get("station")),
                                               "from_document": seems or "a document of no standard form"},
                             keys=keys, page=page, quote=quote, verification=ver))
    for it in data.get("items") or []:
        page, quote, ver = ground(ctx, it.get("quote"), [])
        if ver == "unverified":
            problems.append(f"an item isn't on the page as quoted: {str(it.get('quote'))[:80]!r}")
            continue
        keys = {}
        for key, field in (("invoice_id", "invoice"), ("awb", "awb"), ("contract_id", "agreement")):
            if v.text_or_none(it.get(field)):
                keys[key] = it[field]
        facts.append(FactOut("document_note", {
            "kind": v.choice(it.get("kind"), NOTE_KINDS, "other"), "description": v.text_or_none(it.get("description")),
            "amount": v.text_or_none(it.get("amount")), "currency": v.text_or_none(it.get("currency")),
            "date": v.iso(v.parse_date(it.get("date"))), "seems_to_be": seems, "sender": sender},
            keys=keys, page=page, quote=quote, verification=ver))
    ids = data.get("identifiers") or {}
    if any(ids.get(k) for k in ("invoices", "awbs", "agreements")):
        facts.append(FactOut("document_note", {"kind": "identifiers", "seems_to_be": seems,
                                               "invoices": ids.get("invoices") or [], "awbs": ids.get("awbs") or [],
                                               "agreements": ids.get("agreements") or [], "sender": sender},
                             verification="unverified"))
    return facts, problems


class GeneralReader:
    name = "general"
    version = VERSION

    def read(self, ctx: DocContext, gateway: Gateway) -> ReadOutcome:
        out = ReadOutcome(self.name, self.version)
        data = call(gateway, out, purpose="general", models=config.READER_MODELS, system=SYSTEM, tool_def=TOOL,
                    content=_prompt(ctx), max_tokens=8000)
        facts, problems = build(ctx, data)
        out.facts, out.warnings = facts, out.warnings + problems
        out.doc_type = "other"
        out.status = "partial" if out.warnings else "ok"
        return out
