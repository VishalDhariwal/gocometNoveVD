"""The grader's entry point. It calls exactly these three commands, each in a fresh process:

    python harness.py ingest <batch_dir> --state-dir <dir>
    python harness.py report --as-of YYYY-MM-DD [--mode supervised|autonomous] [--state-dir <dir>] > report.json
    python harness.py feedback <feedback.json> --state-dir <dir>

- ingest: read every .eml in <batch_dir> (attachments inside) into the store under --state-dir.
- report: print one report.json (grader/schema/report.schema.json) on stdout. Only documents received on or before
  --as-of count. --mode defaults to supervised.
- feedback: take in a person's decisions (feedback.json) so later reports use them.

The argument names stay exactly as the grader passes them (grader/validate.py checks). The work lives in the `nova`
package next to this file; see docs/ARCHITECTURE.md. Heavy imports happen inside the commands, so `--help` is instant.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_STATE = ".state"

# The grader runs us from an empty working folder, so make `import nova` work from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def ingest(batch_dir: str, state_dir: str) -> int:
    from nova import cli
    return cli.ingest(batch_dir, state_dir)


def report(as_of: str, mode: str, state_dir: str) -> dict:
    from nova import cli
    return cli.report(as_of, mode, state_dir)


def feedback(feedback_file: str, state_dir: str) -> int:
    from nova import cli
    return cli.feedback(feedback_file, state_dir)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.py", description="Freight invoice audit harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read one batch folder into the state dir")
    p.add_argument("batch_dir")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("report", help="print report.json for a date")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD; only documents received by then count")
    p.add_argument("--mode", choices=["supervised", "autonomous"], default="supervised")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    p = sub.add_parser("feedback", help="take in a person's decisions (feedback.json)")
    p.add_argument("feedback_file")
    p.add_argument("--state-dir", default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    if a.cmd == "ingest":
        return ingest(a.batch_dir, a.state_dir)
    if a.cmd == "report":
        json.dump(report(a.as_of, a.mode, a.state_dir), sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0
    return feedback(a.feedback_file, a.state_dir)


if __name__ == "__main__":
    sys.exit(main())
