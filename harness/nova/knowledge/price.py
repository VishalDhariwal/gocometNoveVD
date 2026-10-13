"""Trace how invoices are priced, as of a date. For debugging, never called by the grader.

    PYTHONPATH=harness python -m nova.knowledge.price --state-dir out/state --as-of 2035-09-30 [--invoice TX-US-2035-0198]
"""
from __future__ import annotations

import argparse
import sys

from .. import normalize as norm
from ..store import Store
from .pricing import InvoicePricing, LinePrice, price_all
from .terms import Terms
from .view import View


def _line(lp: LinePrice) -> str:
    label = str(lp.line.payload.get("label") or "")[:24]
    if lp.status == "priced":
        diff = lp.billed - lp.expected
        mark = f"{diff:+.2f}" if diff else "ok"
        return f"    {lp.code or '?':7} {label:24} {lp.billed:>10} -> {lp.expected:<10} {mark:9} {lp.why}"
    if lp.status == "conflict":
        opts = ", ".join(f"{k}: {v}" for k, v in lp.options.items())
        return f"    {lp.code or '?':7} {label:24} {lp.billed:>10} -> CONFLICT {opts} ({lp.why})"
    if lp.status == "missing":
        return f"    {lp.code or '?':7} {label:24} {lp.billed:>10} -> MISSING {lp.missing.name} ({lp.missing.holder})"
    return f"    {lp.code or '?':7} {label:24} {lp.billed:>10}    {lp.status}: {lp.why}"


def show(ip: InvoicePricing, verbose: bool = False) -> str:
    out = [f"{ip.invoice_id}  {ip.contract or 'no contract'}  {ip.currency or '?'}  ({ip.invoice.doc_ref})"]
    out += [f"  note: {n}" for n in ip.notes]
    for s in ip.shipments:
        pk = f"{s.pickup.date} ({s.pickup.source})" if s.pickup.date else "no pickup date"
        out.append(f"  {s.awb} lane {s.lane} picked up {pk}, {s.weight} kg")
        out += [f"    note: {n}" for n in s.notes]
        for lp in s.lines:
            out.append(_line(lp))
            if verbose:
                out += [f"        {step}" for step in lp.steps]
                out += [f"        cite {src.fact.doc_ref} p{src.fact.page}: {src.fact.quote!r}" for src in lp.sources]
    for lp in ip.other:
        out.append(_line(lp))
    if ip.tax:
        out.append(f"  tax billed {ip.tax.billed}, expected {ip.tax.expected} ({ip.tax.status}: {ip.tax.why})")
    if ip.fx is not None:
        out.append(f"  fx: {getattr(ip.fx, 'value', None) or getattr(ip.fx, 'why', '')}")
    lo, hi = ip.overcharge_range()
    state = "MISSING " + ", ".join(m.name for m in ip.missing()) if ip.missing() else \
        "CONFLICT" if ip.conflicts() else "decided"
    out.append(f"  billed above correct: {lo}" + (f" to {hi}" if hi != lo else "") + f"  [{state}]")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--as-of")
    ap.add_argument("--invoice", help="one invoice number, as printed or normalised")
    ap.add_argument("-v", "--verbose", action="store_true", help="show steps and citations")
    args = ap.parse_args(argv)
    with Store.open(args.state_dir) as store:
        terms = Terms(View.load(store, args.as_of))
        priced = price_all(terms)
    if args.invoice:
        priced = [p for p in priced if p.invoice_id == norm.invoice_id(args.invoice)]
    if not priced:
        print("no invoices", file=sys.stderr)
        return 1
    print("\n\n".join(show(p, args.verbose) for p in sorted(priced, key=lambda p: p.invoice_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
