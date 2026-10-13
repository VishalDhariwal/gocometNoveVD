"""Invoices, statements, charges advices and credit notes, in any layout.

Claude copies the header and every line as printed. Code then:
- parses every number and date (one date format per document, worked out from all its dates);
- grounds each line in the page and checks its printed values are really in that line;
- checks the arithmetic: quantity x rate (or base x %) against each line amount, and the lines against the subtotal.

A line whose own maths doesn't hold is NOT corrected: a carrier's arithmetic slip can itself be an overcharge. It's
recorded (math_ok = false) for the detectors. What does trigger a second read is a line we can't find on the page
or lines that don't add up to the subtotal, which usually means a misread or a missed line.
"""
from __future__ import annotations

from decimal import Decimal

from .. import config
from .. import normalize as norm
from ..llm import Gateway
from ..money import pct_of, to_cents
from . import values as v
from .base import (AUTOMATION_FIELD, COMMON_RULES, QUOTE, STR, STR_LIST, DocContext, FactOut, ReadOutcome, call,
                   ground, injection_facts, tool)

VERSION = "billing-v2"

SYSTEM = COMMON_RULES + """

This is a carrier's billing document: an invoice (whatever it is titled), a statement of account, or a credit note.
Record its own number, its dates, the parties, the contract it is billed under and its currency. Then record EVERY charge line in the order printed, then the subtotal, each tax line and the total.

For each line copy:
- awb: the shipment's air waybill number, if the line has one; house: a house waybill, if the line names one separately
- label: the charge exactly as printed ("Air freight", "Screening fee (adj.)", "Late fee")
- lane: the lane or route; pickup_date: the pickup, collection or shipment date on the line
- quantity: the quantity with its unit as printed ("456 kg", "1 shpt", "3 days", "1 month"), or, for a percentage line, the base amount it is applied to
- unit: what the quantity counts: kg, shipment, piece, day, month, invoice, percent_of_base (a percentage line's base amount) or other
- rate: as printed ("2.62", or "19.41%" for a percentage line)
- amount: the line amount as printed
- period: a billing period the line names (a statement's month)
- refers_to_invoice: an invoice number the line refers to (for example a late fee charged on an earlier invoice)
- reference: anything else the line says it refers to, copied as printed

For a credit note, put the kind of credit, its period and its basis in `credit`.
markers: words on the document that mark this copy, such as copy, duplicate, reminder, re-issued, for your records, pro forma, copied as printed; marker_kinds: the same as choices (copy, duplicate, reminder, for_your_records, reissued, pro_forma, other)."""

LINE = {"type": "object", "properties": {
    "awb": STR, "house": STR, "label": STR, "lane": STR, "pickup_date": STR, "quantity": STR,
    "unit": {"enum": v.LINE_UNITS}, "rate": STR,
    "amount": STR, "period": STR, "refers_to_invoice": STR, "reference": STR, "quote": QUOTE,
}, "required": ["label", "amount", "quote"]}
AMOUNT_LINE = {"type": "object", "properties": {"label": STR, "amount": STR, "quote": QUOTE}, "required": ["amount"]}
TAX_LINE = {"type": "object", "properties": {"label": STR, "base": STR, "rate": STR, "amount": STR, "quote": QUOTE},
            "required": ["amount"]}

TOOL = tool("record_billing_document", "Record what an invoice, statement or credit note says.", {
    "doc_kind": {"enum": ["invoice", "statement", "credit_note", "other"]},
    "number": {"type": "string", "description": "the document's own number exactly as printed"},
    "number_quote": QUOTE,
    "issuer": STR,
    "bill_to": STR,
    "contract_ref": {"type": "string", "description": "the contract or agreement number it is billed under"},
    "currency": {"type": "string", "description": "the currency code as printed, e.g. USD, INR"},
    "document_date": {"type": "string", "description": "the invoice / document / statement / credit date as printed"},
    "due_date": {"type": "string", "description": "the due or payable-by date as printed"},
    "dates_quote": QUOTE,
    "payment_terms": STR,
    "lines": {"type": "array", "items": LINE},
    "subtotal": AMOUNT_LINE,
    "taxes": {"type": "array", "items": TAX_LINE},
    "total": AMOUNT_LINE,
    "markers": STR_LIST,
    "marker_kinds": {"type": "array", "items": {"enum": v.COPY_MARKERS}},
    "credit": {"type": "object", "properties": {
        "credit_type": {"enum": ["rebate", "improvement", "margin", "correction", "other"]},
        "credit_type_as_printed": STR, "period": STR, "basis": STR, "invoices": STR_LIST, "quote": QUOTE}},
    "automation_instructions": AUTOMATION_FIELD,
}, ["doc_kind", "lines", "automation_instructions"])

_CREDIT_CLASS = {"rebate": "rebate", "improvement": "promised_credit", "margin": "margin_step_down"}


def line_math(qty: Decimal | None, unit: str | None, rate: Decimal | None, pct: bool,
              amount: Decimal | None) -> bool | None:
    """Does the line's own arithmetic hold? None when there's nothing to check (no rate, or a % line whose base is
    not printed as an amount, like a late fee shown as '1 month @ 1.86%')."""
    if amount is None or rate is None or qty is None:
        return None
    if pct:
        return None if unit else pct_of(qty, rate) == amount
    return to_cents(qty * rate) == amount


def _prompt(ctx: DocContext) -> str:
    parts = []
    if ctx.parent_email:
        parts.append(f"(Context: attached to an email from {ctx.parent_email.get('from_addr')} with subject "
                     f"{ctx.parent_email.get('subject')!r}.)")
    parts.append(ctx.document_block())
    return "\n".join(parts)


def build(ctx: DocContext, data: dict) -> tuple[list[FactOut], list[str]]:
    """Turn one reading into facts. -> (facts, problems worth a second read)."""
    problems: list[str] = []
    facts = injection_facts(ctx, data.get("automation_instructions"))
    kind = data.get("doc_kind") or "other"
    lines = data.get("lines") or []
    number = v.text_or_none(data.get("number"))
    order = v.date_order([data.get("document_date"), data.get("due_date")] + [ln.get("pickup_date") for ln in lines])

    def date_of(raw):
        d = v.parse_date(raw, order)
        if raw and v.text_or_none(raw) and d is None:
            problems.append(f"couldn't read the date {raw!r} without guessing its format")
        return v.iso(d)

    doc_date, due_date = date_of(data.get("document_date")), date_of(data.get("due_date"))
    currency = (v.text_or_none(data.get("currency")) or "").upper() or None
    contract = norm.code(data["contract_ref"]) if v.text_or_none(data.get("contract_ref")) else None
    invoice_id = norm.invoice_id(number) if number else None

    line_facts: list[FactOut] = []
    amounts: list[Decimal] = []
    math_issues: list[int] = []
    for i, ln in enumerate(lines, start=1):
        amount = v.parse_amount(ln.get("amount"))
        if amount is None:
            problems.append(f"line {i} ({ln.get('label')}) has no readable amount")
            continue
        amounts.append(amount)
        rate_raw = v.text_or_none(ln.get("rate"))
        pct = v.is_pct(rate_raw)
        rate = v.parse_amount(rate_raw)
        qty, unit = v.parse_quantity(v.text_or_none(ln.get("quantity")))
        said = v.choice(ln.get("unit"), v.LINE_UNITS)
        if said is not None:  # Claude's reading of the unit wins; the printed suffix is only a fallback
            unit = None if said in ("percent_of_base", "other") else said
        math_ok = line_math(qty, unit, rate, pct, amount)
        if math_ok is False:
            math_issues.append(i)
        awb_raw, house_raw = v.text_or_none(ln.get("awb")), v.text_or_none(ln.get("house"))
        page, quote, ver = ground(ctx, ln.get("quote"), [ln.get("amount"), awb_raw, house_raw, rate_raw])
        if ver == "unverified":
            problems.append(f"line {i} ({ln.get('label')} {ln.get('amount')}) isn't on the page as quoted")
        refers = v.text_or_none(ln.get("refers_to_invoice"))
        line_facts.append(FactOut(
            "invoice_line",
            {"line_no": i, "label": ln.get("label"), "label_key": norm.label(ln.get("label") or ""),
             "awb": norm.awb(awb_raw) if awb_raw else None, "house": norm.awb(house_raw) if house_raw else None,
             "lane": norm.lane(ln["lane"]) if v.text_or_none(ln.get("lane")) else None,
             "pickup_date": date_of(ln.get("pickup_date")), "pickup_date_printed": v.text_or_none(ln.get("pickup_date")),
             "quantity": qty, "unit": unit, "rate": rate, "rate_is_pct": pct, "amount": amount, "currency": currency,
             "period": v.parse_month(ln.get("period")) or v.text_or_none(ln.get("period")),
             "refers_to_invoice": norm.invoice_id(refers) if refers else None,
             "reference": v.text_or_none(ln.get("reference")), "math_ok": math_ok,
             "document_date": doc_date, "doc_kind": kind},
            keys={"invoice_id": invoice_id, "awb": house_raw or awb_raw,
                  "lane": ln.get("lane") if v.text_or_none(ln.get("lane")) else None, "contract_id": contract},
            page=page, quote=quote, verification=ver))

    subtotal = v.parse_amount((data.get("subtotal") or {}).get("amount"))
    total = v.parse_amount((data.get("total") or {}).get("amount"))
    taxes = [{"label": t.get("label"), "base": v.parse_amount(t.get("base")), "rate": v.parse_amount(t.get("rate")),
              "amount": v.parse_amount(t.get("amount"))} for t in data.get("taxes") or []]
    tax_sum = sum((t["amount"] for t in taxes if t["amount"] is not None), Decimal("0"))
    lines_sum = sum(amounts, Decimal("0"))
    if subtotal is not None:
        adds_up = lines_sum == subtotal
        if not adds_up and kind != "credit_note":
            problems.append(f"the lines add up to {lines_sum} but the subtotal is {subtotal}")
    elif total is not None:
        adds_up = lines_sum + tax_sum == total
        if not adds_up and kind != "credit_note" and lines:
            problems.append(f"the lines and taxes add up to {lines_sum + tax_sum} but the total is {total}")
    else:
        adds_up = None
    if subtotal is not None and total is not None and subtotal + tax_sum != total:
        problems.append(f"subtotal {subtotal} plus tax {tax_sum} isn't the total {total}")

    page, quote, ver = ground(ctx, data.get("number_quote"), [number])
    header = {"number_as_printed": number, "doc_kind": kind, "issuer": v.text_or_none(data.get("issuer")),
              "bill_to": v.text_or_none(data.get("bill_to")), "contract_ref": contract, "currency": currency,
              "document_date": doc_date, "due_date": due_date, "date_order": order,
              "payment_terms": v.text_or_none(data.get("payment_terms")), "subtotal": subtotal, "taxes": taxes,
              "total": total, "lines_sum": lines_sum, "adds_up": adds_up, "line_count": len(lines),
              "math_issue_lines": math_issues, "markers": [m for m in data.get("markers") or [] if m],
              "marker_kinds": sorted({m for m in data.get("marker_kinds") or [] if m in v.COPY_MARKERS})}

    if kind == "credit_note":
        credit = data.get("credit") or {}
        ctype = credit.get("credit_type") or "other"
        header.update({"credit_type": ctype, "credit_class": _CREDIT_CLASS.get(ctype, ctype),
                       "credit_type_as_printed": v.text_or_none(credit.get("credit_type_as_printed")),
                       "period": v.text_or_none(credit.get("period")), "basis": v.text_or_none(credit.get("basis")),
                       "refers_to_invoices": [norm.invoice_id(x) for x in credit.get("invoices") or [] if x],
                       "amount": total if total is not None else lines_sum,
                       "lines": [f.payload for f in line_facts]})
        if header["amount"] is None:
            problems.append("credit note without a readable amount")
        refs = header["refers_to_invoices"]
        facts.append(FactOut("credit_note", header,
                             keys={"contract_id": contract, "invoice_id": refs[0] if len(refs) == 1 else None,
                                   "period": header["period"]},
                             page=page, quote=quote, verification=ver))
        return facts, problems

    if kind == "other":
        return facts, problems
    if not invoice_id:
        problems.append("no document number found")
        return facts, problems
    facts.append(FactOut("invoice", header, keys={"invoice_id": invoice_id, "contract_id": contract}, page=page,
                         quote=quote, verification=ver))
    return facts + line_facts, problems


def _feedback(problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems[:25])
    return ("\n\nA first reading of this document had these problems:\n" + listed +
            "\nRead the document again carefully and record it again. Copy every line exactly as printed and don't "
            "skip any. If the document's own arithmetic is wrong, keep its printed figures as they are.")


class BillingReader:
    name = "billing"
    version = VERSION

    def read(self, ctx: DocContext, gateway: Gateway) -> ReadOutcome:
        out = ReadOutcome(self.name, self.version)
        prompt = _prompt(ctx)
        data = call(gateway, out, purpose="billing", models=config.READER_MODELS, system=SYSTEM, tool_def=TOOL,
                    content=prompt, max_tokens=12000)
        facts, problems = build(ctx, data)
        if problems:
            again = call(gateway, out, purpose="billing", models=config.READER_MODELS, system=SYSTEM, tool_def=TOOL,
                         content=prompt + _feedback(problems), max_tokens=12000)
            facts2, problems2 = build(ctx, again)
            if len(problems2) < len(problems):
                data, facts, problems = again, facts2, problems2
        out.doc_type = data.get("doc_kind")
        out.facts = facts
        out.warnings = problems
        out.status = "partial" if problems else "ok"
        return out
