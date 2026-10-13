"""Builds the report for one as_of date and mode, and checks it against the grader's schema before it's printed.

Right now it returns an empty, valid report: the detectors, the policy gate and the writer fill it in later phases
(docs/ARCHITECTURE.md, section 13). The schema check and the never-crash behaviour are here from day one.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema

from ..store import Store

SCHEMA_VERSION = "1.0"


def empty_report(as_of: str, mode: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "as_of": as_of, "mode": mode, "findings": [],
            "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []}}


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    # A copy of grader/schema/report.schema.json; tests/test_intake.py asserts the two stay identical.
    schema = json.loads((Path(__file__).parent / "report.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema)


def schema_errors(report: dict) -> list[str]:
    return [f"{'/'.join(map(str, e.path)) or '(top level)'}: {e.message}" for e in _validator().iter_errors(report)]


def build_report(store: Store, as_of: str, mode: str, report_id: int) -> dict:
    return empty_report(as_of, mode)
