"""A compact summary of what the readers stored, to paste back after a live run.

    PYTHONPATH=harness python -m nova.readers.check --state-dir .state
"""
from __future__ import annotations

import argparse
import sys

from ..store import Store


def summary(store: Store, *, max_warnings: int = 30) -> str:
    q = lambda sql, *a: store.con.execute(sql, a).fetchall()  # noqa: E731
    out = []
    out.append("documents: " + ", ".join(f"{r[0]}/{r[1] or '-'}={r[2]}" for r in
                                         q("SELECT format, status, COUNT(*) FROM documents GROUP BY 1, 2 ORDER BY 1, 2")))
    out.append("doc types: " + ", ".join(f"{r[0]}={r[1]}" for r in
                                         q("SELECT COALESCE(doc_type, '?'), COUNT(*) FROM documents "
                                           "WHERE format != 'eml' GROUP BY 1 ORDER BY 2 DESC")))
    out.append("facts by kind (verified/repaired/structured/vision/unverified):")
    for kind, ver, structured, vision, rep, unver in q(
            "SELECT kind, SUM(verification = 'verified'), SUM(verification = 'structured'), "
            "SUM(verification = 'vision'), SUM(verification = 'repaired'), SUM(verification = 'unverified') "
            "FROM current_facts GROUP BY kind ORDER BY kind"):
        out.append(f"  {kind:20} {ver}/{rep}/{structured}/{vision}/{unver}")
    unver = q("SELECT doc_ref, kind, substr(quote, 1, 70) FROM current_facts WHERE verification = 'unverified' "
              "LIMIT 15")
    if unver:
        out.append("unverified facts (first 15):")
        out += [f"  {r[0]} {r[1]}: {r[2]!r}" for r in unver]
    bad_math = q("SELECT doc_ref, json_extract(payload, '$.math_issue_lines'), json_extract(payload, '$.adds_up') "
                 "FROM current_facts WHERE kind = 'invoice' AND (json_extract(payload, '$.adds_up') = 0 OR "
                 "json_array_length(json_extract(payload, '$.math_issue_lines')) > 0)")
    if bad_math:
        out.append("invoices whose own arithmetic doesn't hold (line numbers, adds up):")
        out += [f"  {r[0]}: lines {r[1]}, adds up {bool(r[2]) if r[2] is not None else '?'}" for r in bad_math]
    warns = q("SELECT message FROM run_log WHERE level != 'info' ORDER BY id DESC LIMIT ?", max_warnings)
    if warns:
        out.append(f"latest warnings ({len(warns)}):")
        out += [f"  {r[0][:200]}" for r in warns]
    for r in q("SELECT command, status, api_calls, input_tokens, output_tokens, "
               "ROUND((julianday(finished_at) - julianday(started_at)) * 86400, 1) FROM runs ORDER BY id"):
        out.append(f"run {r[0]:8} {r[1]:7} calls={r[2]} tokens in/out={r[3]}/{r[4]} seconds={r[5]}")
    return "\n".join(out)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nova.readers.check")
    ap.add_argument("--state-dir", default=".state")
    a = ap.parse_args(argv)
    with Store.open(a.state_dir) as store:
        print(summary(store))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
