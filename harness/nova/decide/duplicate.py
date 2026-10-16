"""Duplicates, two ways (GLOSSARY "Duplicates"):

1. The same invoice number sent again: a finding only when the payment register shows the first copy paid or
   scheduled before the resend arrived, and the resend asks for payment again without calling itself a copy or
   "for your records". The amount is the invoice total.
2. The same charge on two invoice numbers: same waybill (house level), same Annex E code, same amount. The invoice
   with the later invoice date carries the finding, for the repeated lines only. Different houses under one master
   are different shipments. Credited in full and billed again is fine; a partial credit comes off the overlap.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from ..knowledge.terms import Source, Terms
from ..knowledge.view import day
from .common import (ZERO, Claims, Verdict, amount_of, contract_of, credit_notes, invoice_date,
                     lines_of, to_usd)

PAID = ("paid", "scheduled")
NOT_A_REQUEST = {"copy", "duplicate", "for_your_records"}


def detect(terms: Terms, claims: Claims, headers: dict) -> list[Verdict]:
    out: dict[str, Verdict] = {}
    for v in resends(terms):
        out[v.key["invoice_id"]] = v
    for v in repeated_lines(terms, headers):
        if v.key["invoice_id"] not in out:
            out[v.key["invoice_id"]] = v
    for v in out.values():
        if v.verdict == "finding":
            claims.claim("duplicate", v.lines)
    return list(out.values())


# ---- 1. the same invoice number again ---------------------------------------------------------------------------
def resends(terms: Terms) -> list[Verdict]:
    view = terms.view
    copies: dict[str, list] = defaultdict(list)
    for f in view.kind("invoice"):
        if f.payload.get("doc_kind") != "credit_note":
            copies[f.invoice_id].append(f)
    out = []
    for inv_id, fs in sorted(copies.items()):
        fs.sort(key=lambda f: (f.known_at, f.id))
        docs = {f.doc_id for f in fs}
        if len(docs) < 2:
            continue
        first = fs[0]
        for resend in fs[1:]:
            if resend.doc_id == first.doc_id:
                continue
            v = _resend(terms, first, resend)
            if v is not None:
                out.append(v)
                break
    return out


def _resend(terms: Terms, first, resend) -> Verdict | None:
    view = terms.view
    inv_id, arrived = resend.invoice_id, day(resend.known_at)
    paid = [e for e in view.get("ledger_entry", invoice_id=inv_id) if e.payload.get("status") in PAID
            and (day(e.payload.get("paid_date")) or day(e.payload.get("register_as_of")) or day(e.known_at)) <= arrived]
    info = view.doc(resend.doc_id)
    docs = {resend.doc_id, info.parent_id if info else None}
    asks = [r for r in view.get("payment_request", invoice_id=inv_id)
            if r.doc_id in docs and not r.payload.get("from_quoted_text")]
    markers = set(resend.payload.get("marker_kinds") or []) | {r.payload.get("marker_kind") for r in asks}
    contract = contract_of(terms, first)
    v = Verdict("duplicate", {"invoice_id": inv_id}, "no_finding", contract=contract,
                currency=resend.payload.get("currency"))
    if not paid:
        v.why = "sent again, but the register doesn't show the first copy paid or scheduled before that"
        return v
    if not any(r.payload.get("asks_for_payment") for r in asks):
        v.why = "sent again without asking for payment"
        return v
    if markers & NOT_A_REQUEST:
        v.why = f"sent again marked {', '.join(sorted(markers & NOT_A_REQUEST))}"
        return v
    total = resend.payload.get("total") or resend.payload.get("lines_sum")
    native = Decimal(str(total)) if total is not None else sum((amount_of(f) for f in lines_of(view, resend)), ZERO)
    usd, fx = to_usd(terms, contract, v.currency, native, invoice_date(resend))
    if usd is None:
        v.why = "the invoice total can't be converted to USD"
        return v
    v.verdict, v.remedy = "finding", "dispute"
    v.amount_native, v.amount_usd = native, usd
    v.why = "the invoice was paid and then sent again asking for payment"
    v.lines = [f.id for f in lines_of(view, resend)]
    v.cite(Source(resend, 3, "the copy sent again"), *(Source(r, 3, "asks for payment") for r in asks[:1]),
           Source(paid[-1], 3, "the register shows it paid"), Source(first, 3, "the first copy"))
    v.steps.append(f"First copy {first.doc_ref}; the payment register shows it {paid[-1].payload.get('status')} "
                   f"{paid[-1].payload.get('paid_date') or ''}; sent again {resend.doc_ref} asking for payment")
    v.checks.update(fx=fx.sources[0] if fx is not None else None)
    return v


# ---- 2. the same charge on two invoice numbers ------------------------------------------------------------------
def repeated_lines(terms: Terms, headers: dict) -> list[Verdict]:
    view = terms.view
    sigs: dict[tuple, list] = defaultdict(list)
    for inv_id, h in headers.items():
        contract = contract_of(terms, h)
        for f in lines_of(view, h):
            code = terms.charge_code(contract, f.payload.get("label"))
            amount = amount_of(f)
            if code is None or code in ("TAX", "LATE") or amount <= 0:
                continue
            period = f.payload.get("period")
            sig = ("awb", f.awb, code, amount) if f.awb else ("period", period, code, amount) if period else None
            if sig is not None:
                sigs[sig].append((h, f, code))
    dups: dict[str, list] = defaultdict(list)
    for sig, items in sigs.items():
        if len({h.invoice_id for h, _, _ in items}) < 2:
            continue
        items.sort(key=lambda x: (invoice_date(x[0]) or date.max, x[0].known_at, x[0].id, x[1].id))
        original = items[0]
        for h, f, code in items[1:]:
            if h.invoice_id != original[0].invoice_id:
                dups[h.invoice_id].append((f, code, original))
    out = []
    for inv_id, found in sorted(dups.items()):
        out.append(_repeated(terms, headers[inv_id], found))
    return out


def _credited(view, invoice_id: str, contract) -> list:
    return [c for c in credit_notes(view, contract)
            if c.invoice_id == invoice_id or invoice_id in (c.payload.get("refers_to_invoices") or [])]


def _repeated(terms: Terms, header, found: list) -> Verdict:
    view = terms.view
    contract = contract_of(terms, header)
    v = Verdict("duplicate", {"invoice_id": header.invoice_id}, "no_finding", contract=contract,
                currency=header.payload.get("currency"))
    keep, overlap = [], ZERO
    for f, code, (orig_h, orig_f, _) in found:
        orig_credits = sum((amount_of(c).copy_abs() for c in _credited(view, orig_h.invoice_id, contract)), ZERO)
        orig_total = Decimal(str(orig_h.payload.get("total") or orig_h.payload.get("lines_sum") or 0))
        if orig_total and orig_credits >= orig_total:
            v.steps.append(f"{orig_h.invoice_id} was credited in full, so billing {f.awb} {code} again is fine")
            continue
        keep.append((f, code, orig_h, orig_f))
        overlap += amount_of(f)
    if not keep:
        v.why = "the earlier bill was credited in full"
        return v
    credit = sum((amount_of(c).copy_abs() for c in _credited(view, header.invoice_id, contract)), ZERO)
    for _, _, orig_h, _ in keep:   # a partial credit of the earlier bill for these same shipments
        for c in _credited(view, orig_h.invoice_id, contract):
            for ln in c.payload.get("lines") or []:
                if ln.get("awb") and any(ln.get("awb") in (f.awb, f.payload.get("awb")) for f, *_ in keep):
                    credit += Decimal(str(ln.get("amount") or 0)).copy_abs()
    native = max(ZERO, overlap - credit)
    for f, code, orig_h, orig_f in keep:
        v.steps.append(f"{f.payload.get('label')} ({code}) on {f.awb or f.payload.get('period')}: {amount_of(f)}, "
                       f"already billed on {orig_h.invoice_id} as '{orig_f.payload.get('label')}' "
                       f"({amount_of(orig_f)}), same waybill, charge code and amount")
    v.steps.append(f"Billed twice: {overlap}" + (f", less credit notes {credit}" if credit else "") + f" = {native}")
    if native <= 0:
        v.why = "the repeated charges were credited"
        return v
    usd, fx = to_usd(terms, contract, v.currency, native, invoice_date(header))
    if usd is None:
        v.why = "the amount can't be converted to USD"
        return v
    v.verdict, v.remedy, v.why = "finding", "dispute", "the same charge was billed on an earlier invoice"
    v.amount_native, v.amount_usd = native, usd
    v.lines = [f.id for f, *_ in keep]
    for f, code, orig_h, orig_f in keep:
        v.cite(Source(f, 3, "billed again"), Source(orig_f, 3, f"first billed on {orig_h.invoice_id}"))
        if (f.payload.get("label_key") or "") != (orig_f.payload.get("label_key") or ""):
            alias = next((a for a in view.get("charge_alias", contract_id=contract)
                          if f.payload.get("label_key") in (a.payload.get("label_keys") or [])), None)
            v.cite(Source(alias, 1, f"Annex E maps the label to {code}") if alias else None)
    v.checks.update(fx=fx.sources[0] if fx is not None else None)
    return v
