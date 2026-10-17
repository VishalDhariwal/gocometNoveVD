"""Late fees (GLOSSARY "Late fees").

Not owed: for each billed late fee, work out what was owed on the invoice it charges for. days_late = paid date -
due date, where the due date is the one the Schedule 1 payment-terms person extended it to, if they did. No fee
within the grace days; otherwise ceil(days_late / 30) months, each fee rounded half up, on a base that compounds or
not as the contract says. Late-fee lines for the same invoice are matched to the fee months in billing order; what
a line bills above its months' fees is not owed. If the invoice isn't paid yet, lateness runs to the date the fee
was billed.

The invoice a fee is charged on is the line's refers_to_invoice; when the reader left it out, an invoice number we hold
that the line's own words name ("Late fee on TX-US-2035-0202 ...") is used, if exactly one is named.

Risk: an unpaid invoice whose due date is 1 to 14 days after as_of. Amount: one month's fee on what's still open,
less what we're disputing on that invoice, in the invoice's currency, converted once.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from ..knowledge.terms import Decided, Missing, Source, Terms
from ..knowledge.view import dec, day
from ..links import find_known
from ..money import pct_of
from .common import (ZERO, Claims, Verdict, amount_of, contract_of, fee_schedule, invoice_date, ledger, lines_of,
                     missing_doc, months_late, to_usd)

OPEN_STATUSES = ("open", "disputed", "other", None)


def due_date(terms: Terms, invoice_id: str, contract: str | None, header=None) -> tuple[date | None, Source | None]:
    """The invoice's due date: an authorised extension first, then the register, the invoice, the payment terms."""
    view = terms.view
    granted = []
    for f in view.get("due_date_extension", invoice_id=invoice_id):
        if contract and terms.auth.of_email_fact(f, contract, "payment_terms").level <= 2 and \
                day(f.payload.get("new_due_date")):
            granted.append(f)
    if granted:
        f = max(granted, key=lambda f: (f.known_at, f.id))
        return day(f.payload["new_due_date"]), Source(f, 2, "due date extended by the payment-terms contact")
    row = ledger(view, invoice_id)
    if row is not None and day(row.payload.get("due_date")):
        return day(row.payload["due_date"]), Source(row, 3, "due date in the register")
    if header is not None and day(header.payload.get("due_date")):
        return day(header.payload["due_date"]), Source(header, 3, "due date on the invoice")
    if header is not None and contract:
        terms_rule = terms.rule(contract, "payment_terms")
        days = terms_rule.value.get("days") if isinstance(terms_rule, Decided) else None
        if isinstance(days, int) and invoice_date(header):
            return invoice_date(header) + timedelta(days=days), terms_rule.sources[0]
    return None, None


def _late_rule(terms: Terms, contract: str | None):
    r = terms.rule(contract, "late_fee") if contract else None
    if not isinstance(r, Decided) or dec(r.value.get("rate_pct")) is None:
        return None
    return r


# ---- not owed --------------------------------------------------------------------------------------------------
def named_invoice(view, line, carrier: str | None) -> str | None:
    """The invoice a fee line charges for, from its own words, when the reader didn't fill refers_to_invoice: one
    invoice number we hold (an invoice or a register row), other than the invoice carrying the line."""
    known = {f.invoice_id for f in view.kind("invoice") if f.invoice_id}
    known |= {f.invoice_id for f in view.kind("ledger_entry") if f.invoice_id}
    known.discard(carrier)
    p = line.payload
    found = find_known(" ".join(str(x) for x in (p.get("reference"), p.get("label"), line.quote) if x), known)
    return found[0] if len(found) == 1 else None


def not_owed(terms: Terms, claims: Claims, headers: dict) -> list[Verdict]:
    view = terms.view
    by_base: dict[str, list] = defaultdict(list)
    for h in headers.values():
        contract = contract_of(terms, h)
        for f in lines_of(view, h):
            if terms.charge_code(contract, f.payload.get("label")) == "LATE" and not claims.taken(f.id):
                base = f.payload.get("refers_to_invoice") or named_invoice(view, f, h.invoice_id)
                if base:
                    by_base[base].append((h, f, contract))
    per_invoice: dict[str, list] = defaultdict(list)
    for base, items in by_base.items():
        items.sort(key=lambda x: (invoice_date(x[0]) or date.max, x[0].known_at, x[1].payload.get("line_no") or 0))
        for h, f, owed, ctx in _allocate(terms, base, items):
            per_invoice[h.invoice_id].append((h, f, owed, ctx))
    out = []
    for inv_id, rows in sorted(per_invoice.items()):
        v = _verdict(terms, headers[inv_id], rows)
        if v.verdict == "finding":
            claims.claim("late_fee_not_owed", v.lines)
        out.append(v)
    return out


def _allocate(terms: Terms, base: str, items: list):
    """-> (carrying invoice, fee line, fee owed for it or None if not judged, context) per late-fee line."""
    view = terms.view
    contract = items[0][2]
    rule = _late_rule(terms, contract)
    base_header = next((f for f in view.kind("invoice") if f.invoice_id == base), None)
    due, due_src = due_date(terms, base, contract, base_header)
    row = ledger(view, base)
    principal = dec(row.payload.get("amount")) if row else None
    if principal is None and base_header is not None:
        principal = dec(base_header.payload.get("total"))
    if rule is None or due is None or principal is None:
        why = "no late-fee clause" if rule is None else "no due date" if due is None else "no amount for the invoice"
        return [(h, f, None, {"why": why}) for h, f, _ in items]
    if row is not None and row.payload.get("status") == "disputed":
        return [(h, f, None, {"why": f"{base} is disputed in the register"}) for h, f, _ in items]
    paid = day(row.payload.get("paid_date")) if row is not None and row.payload.get("status") == "paid" else None
    rate, grace = dec(rule.value["rate_pct"]), rule.value.get("grace_days") or 0
    compounding = bool(rule.value.get("compounding"))
    out, k = [], 0
    for h, f, _ in items:
        end = paid or invoice_date(h)
        days_late = (end - due).days
        n = months_late(days_late) if days_late > grace else 0
        fees = fee_schedule(principal, rate, n, compounding)
        months = int(dec(f.payload.get("quantity")) or 1) if f.payload.get("unit") == "month" else 1
        owed = sum(fees[k:k + months], ZERO)
        k += months
        ctx = {"due": due, "due_src": due_src, "paid": paid, "row": row, "rule": rule, "days_late": days_late,
               "grace": grace, "months": n, "fees": fees, "principal": principal, "base": base}
        out.append((h, f, owed, ctx))
    return out


def _verdict(terms: Terms, header, rows: list) -> Verdict:
    contract = rows[0][3].get("rule").sources[0].fact.contract_id if rows[0][3].get("rule") else None
    v = Verdict("late_fee_not_owed", {"invoice_id": header.invoice_id}, "no_finding",
                contract=contract or contract_of(terms, header), currency=header.payload.get("currency"))
    over_lines, native = [], ZERO
    for h, f, owed, ctx in rows:
        if owed is None:
            v.steps.append(f"{f.payload.get('label')} on {ctx.get('base', '?')}: not judged ({ctx['why']})")
            continue
        billed = amount_of(f)
        v.steps.append(f"{ctx['base']}: due {ctx['due']}, " + (f"paid {ctx['paid']}" if ctx["paid"] else "not paid")
                       + f", {ctx['days_late']} days late, grace {ctx['grace']}, {ctx['months']} fee month(s) "
                       f"{[str(x) for x in ctx['fees']]}; this line bills {billed}, owed {owed}")
        if billed > owed:
            over_lines.append((f, ctx))
            native += billed - owed
    if native <= 0:
        v.why = "the late fees billed were owed" if rows and rows[0][2] is not None else "not judged"
        return v
    usd, fx = to_usd(terms, v.contract, v.currency, native, invoice_date(header))
    if usd is None:
        if isinstance(fx, Missing):
            v.verdict, v.remedy, v.missing_doc, v.why = "cannot_determine", "request_document", missing_doc(fx), fx.why
        return v
    v.verdict, v.remedy, v.why = "finding", "dispute", "billed a late fee that wasn't owed"
    v.amount_native, v.amount_usd = native, usd
    v.lines = [f.id for f, _ in over_lines]
    for f, ctx in over_lines:
        v.cite(Source(f, 3, "the late fee billed"), ctx["due_src"],
               Source(ctx["row"], 3, "the register") if ctx["row"] is not None else None, ctx["rule"].sources[0])
        if ctx["due_src"] is not None and ctx["due_src"].level == 2:
            v.checks.setdefault("term_levels", []).append(2)
    v.checks.update(fx=fx.sources[0] if fx is not None else None)
    return v


# ---- risk ------------------------------------------------------------------------------------------------------
def risks(terms: Terms, headers: dict, disputed: dict[str, Decimal]) -> list[Verdict]:
    view, as_of = terms.view, terms.view.as_of_date
    out = []
    for inv_id, h in sorted(headers.items()):
        row = ledger(view, inv_id)
        if row is not None and row.payload.get("status") not in OPEN_STATUSES:
            continue
        contract = contract_of(terms, h)
        due, due_src = due_date(terms, inv_id, contract, h)
        if due is None or as_of is None or not 0 < (due - as_of).days <= 14:
            continue
        rule = _late_rule(terms, contract)
        v = Verdict("late_fee_risk", {"invoice_id": inv_id}, "no_finding", deadline=due.isoformat(),
                    contract=contract, currency=h.payload.get("currency"))
        if rule is None:
            v.why = "no late-fee clause"
            out.append(v)
            continue
        open_amount = dec(row.payload.get("amount")) if row is not None else None
        if open_amount is None:
            open_amount = dec(h.payload.get("total")) or dec(h.payload.get("lines_sum")) or ZERO
        principal = max(ZERO, open_amount - disputed.get(inv_id, ZERO))
        fee = pct_of(principal, dec(rule.value["rate_pct"]))
        v.steps.append(f"open {open_amount}, disputing {disputed.get(inv_id, ZERO)}, one month at "
                       f"{rule.value['rate_pct']}% = {fee}; due {due}, {(due - as_of).days} days after {as_of}")
        if fee <= 0:
            v.why = "nothing left to charge a fee on"
            out.append(v)
            continue
        usd, fx = to_usd(terms, contract, v.currency, fee, invoice_date(h))
        if usd is None:
            if isinstance(fx, Missing):
                v.verdict, v.remedy, v.missing_doc, v.why = "cannot_determine", "request_document", missing_doc(fx), fx.why
            out.append(v)
            continue
        v.verdict, v.remedy, v.why = "finding", "payment_reminder", f"unpaid and due {due}"
        v.amount_native, v.amount_usd = fee, usd
        v.cite(due_src, Source(row, 3, "open in the register") if row is not None else Source(h, 3, "the invoice"),
               rule.sources[0])
        out.append(v)
    return out
