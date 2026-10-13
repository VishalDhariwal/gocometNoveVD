"""Print every verdict as of a date. For debugging, never called by the grader.

    PYTHONPATH=harness python -m nova.decide.audit --state-dir out/state --as-of 2035-09-30 [--all] [-v]
"""
from __future__ import annotations

import argparse

from ..knowledge.view import View
from ..store import Store
from .adjudicate import adjudicate
from .common import Verdict


def show(v: Verdict, verbose: bool = False) -> str:
    money = (f"${v.amount_usd}" if v.amount_usd is not None else
             f"${v.amount_range[0]}-${v.amount_range[1]}" if v.amount_range else "")
    extra = f" deadline {v.deadline}" if v.deadline else ""
    if v.missing_doc:
        extra += f" missing {v.missing_doc['name']!r} from {v.missing_doc['holder']}"
    head = f"{v.finding_key:52} {v.verdict:16} {money:>14}{extra}  [{v.remedy or '-'}] {v.why}"
    if v.feedback:
        head += f" (feedback {', '.join(v.feedback)})"
    if not verbose:
        return head
    rows = [head] + [f"    {s}" for s in v.steps]
    rows += [f"    cite {e.fact.doc_ref} p{e.fact.page or 1}: {e.fact.quote!r}" for e in v.evidence]
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--as-of")
    ap.add_argument("--all", action="store_true", help="include no_finding verdicts")
    ap.add_argument("-v", "--verbose", action="store_true", help="show steps and citations")
    args = ap.parse_args(argv)
    with Store.open(args.state_dir) as store:
        verdicts = adjudicate(View.load(store, args.as_of))
    shown = [v for v in verdicts if args.all or v.verdict != "no_finding"]
    for v in sorted(shown, key=lambda v: (v.cls, v.finding_key)):
        print(show(v, args.verbose))
    counts = {k: sum(1 for v in verdicts if v.verdict == k) for k in ("finding", "conflict", "cannot_determine",
                                                                       "no_finding")}
    print(f"-- {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
