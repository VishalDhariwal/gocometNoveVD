"""Overcharge: billed above what the contract says, summed over the invoice's lines.

One verdict per invoice, in this order (the glossary's rule that a key is only ever one of them):
  cannot_determine  a line depends on a document a held document names and we don't hold (or the month's FX row)
  conflict          a line's rate is in an unranked conflict and the options give different money
  finding           billed above correct by more than zero
  no_finding        everything we could price is at or below the contract price (undercharges never count)
Lines another class already claimed (duplicate, waived, late fee) are left out.
"""
from __future__ import annotations

from decimal import Decimal

from ..knowledge.pricing import InvoicePricing, LinePrice
from ..knowledge.terms import Missing, Terms
from .common import ZERO, Claims, Verdict, invoice_date, missing_doc, to_usd


def _range(ip: InvoicePricing, free: set[int]) -> tuple[Decimal, Decimal]:
    lo = hi = sum((lp.over() for lp in ip.other if lp.line.id in free), ZERO) + (ip.tax.over() if ip.tax else ZERO)
    for s in ip.shipments:
        lps = [lp for lp in s.lines if lp.line.id in free]
        if s.scenarios:
            vals = [sum((lp.over(sc) for lp in lps), ZERO) for sc in s.scenarios]
            a, b = min(vals), max(vals)
        else:
            a = b = sum((lp.over() for lp in lps), ZERO)
        lo, hi = lo + a, hi + b
    return lo, hi


def _most(lp: LinePrice) -> Decimal:
    """The most this line could be billed above correct (over every conflict option)."""
    if lp.status == "conflict":
        return max((max(ZERO, lp.billed - e) for e in lp.options.values()), default=ZERO)
    return lp.over()


def detect(terms: Terms, claims: Claims, priced: dict[str, InvoicePricing]) -> list[Verdict]:
    out = []
    for inv_id, ip in sorted(priced.items()):
        free = {lp.line.id for lp in ip.lines() if not claims.taken(lp.line.id)}
        lines = [lp for lp in ip.lines() if lp.line.id in free]
        if not any(lp.status in ("priced", "conflict", "missing") for lp in lines):
            continue
        v = _verdict(terms, ip, lines, free)
        if v.verdict == "finding":
            claims.claim("overcharge", v.lines)
        out.append(v)
    return out


def _verdict(terms: Terms, ip: InvoicePricing, lines: list[LinePrice], free: set[int]) -> Verdict:
    v = Verdict("overcharge", {"invoice_id": ip.invoice_id}, "no_finding", contract=ip.contract, currency=ip.currency)
    on = invoice_date(ip.invoice)
    by_line = {lp.line.id: s for s in ip.shipments for lp in s.lines}
    missing = [lp for lp in lines if lp.status == "missing" and lp.missing]
    conflicts = [lp for lp in lines if lp.status == "conflict"]
    lo, hi = _range(ip, free)
    v.steps.append(f"billed above correct: {lo}" + (f" to {hi}" if hi != lo else "") + f" {ip.currency or ''}".rstrip())
    for lp in lines:
        v.learn(*lp.feedback)

    if missing:
        m = missing[0].missing
        v.verdict, v.remedy, v.missing_doc, v.why = "cannot_determine", "request_document", missing_doc(m), m.why
        v.cite(*m.sources)
        for lp in missing:
            v.cite(lp.sources[0])
        v.checks.update(missing_holder=m.holder)
        return v

    over = sorted((lp for lp in lines if _most(lp) > 0), key=lambda lp: -_most(lp))
    if hi <= 0:
        v.why = "nothing billed above the contract price"
        return v

    usd_hi, fx = to_usd(terms, ip.contract, ip.currency, hi, on)
    if usd_hi is None:
        if isinstance(fx, Missing):
            v.verdict, v.remedy, v.missing_doc, v.why = "cannot_determine", "request_document", missing_doc(fx), fx.why
            v.cite(*fx.sources)
            v.cite(*(lp.sources[0] for lp in over))
            v.checks.update(missing_holder=fx.holder)
        else:
            v.why = f"can't convert {ip.currency} to USD"
        return v
    usd_lo, _ = to_usd(terms, ip.contract, ip.currency, lo, on)
    if fx is not None:
        v.steps.append(f"converted at {fx.value} {ip.currency} per USD ({fx.label})")

    for lp in over:
        for s in lp.sources:
            v.cite(s)
    v.lines = [lp.line.id for lp in over]
    if ip.tax and ip.tax.over() > 0:
        v.cite(*ip.tax.sources)
        v.steps.append(f"tax billed {ip.tax.billed}, should be {ip.tax.expected}: {ip.tax.why}")
    v.checks.update(
        pickup_sources=sorted({by_line[lp.line.id].pickup.source or "none" for lp in over if lp.line.id in by_line}),
        lanes=sorted({lp.line.lane for lp in over if lp.line.lane}),
        term_levels=sorted({s.level for lp in over for s in lp.sources[1:]}),
        fx=fx.sources[0] if fx is not None else None)
    for lp in over:
        v.steps += [f"{lp.code} {lp.line.payload.get('label')}: billed {lp.billed}, {lp.why}"] + lp.steps

    if conflicts and lo != hi:
        c = next(s.conflict for s in ip.shipments if s.conflict is not None)
        v.verdict, v.remedy, v.why = "conflict", "escalate", c.why
        v.amount_range = (max(usd_lo, ZERO), usd_hi)
        v.cite(*c.sources)
        return v
    v.verdict, v.remedy, v.why = "finding", "dispute", "billed above the contract price"
    v.amount_native, v.amount_usd = hi, usd_hi
    return v
