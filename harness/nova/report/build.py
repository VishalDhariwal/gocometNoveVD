"""Builds the report for one as_of date and mode, and checks it against the grader's schema before it's printed.

verdicts (decide/adjudicate) -> citations re-checked (report/evidence) -> action gate (decide/policy) -> confidence
-> items with stable ids, supersedes and retro -> schema check -> items recorded for the next report -> drafts.

- Only finding, conflict and cannot_determine are printed; no_finding stays in the audit (the grader treats an
  absent key and a no_finding the same, and fewer items means less noise for a person).
- Ids are `class:key` while the answer stays the same. When a new document changes the verdict, amount, range or
  deadline, the id gets a version (`~2`) and `supersedes` names the id printed before, per mode.
- `retro: true`: a key we never printed, that only exists now because a backdated document arrived (effective
  before our last report, received after it) and changed an invoice we already held.
- Keys that should be gone simply aren't produced: everything is recomputed from the facts each time.
- `learned_at` only when feedback changed the answer: every key is also worked out with no feedback at all, and an
  item carries learned_at when its verdict, amount, range, deadline or action differs (or it only exists because
  of feedback), naming the decision behind it.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

import jsonschema

from .. import normalize as norm
from ..decide import confidence, policy
from ..decide.adjudicate import adjudicate
from ..decide.common import Verdict, invoice_headers
from ..jsonx import loads
from ..knowledge.terms import Terms
from ..knowledge.view import View, day
from ..store import Store
from .evidence import Citations, norm as norm_text
from .explain import explanation, role

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
PRESENT = ("finding", "conflict", "cannot_determine")
INVOICE_CLASSES = ("overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed")
PORTFOLIO = {"cashback": "cashback", "expiry": "expiry", "late_fee_risk": "late_fee_risk"}
KEY_FIELDS = {"cashback": ("contract_id", "period", "credit_type"), "expiry": ("contract_id",)}
BACKDATED_KINDS = {"rate", "accessorial", "fuel_rate", "contract_term", "fx_rate", "instrument", "charge_alias"}


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


def _key_str(v: Verdict) -> str:
    return ":".join(str(v.key[k]) for k in KEY_FIELDS.get(v.cls, ("invoice_id",)))


def _money(x) -> str | None:
    return None if x is None else f"{x:.2f}"


def _signature(v: Verdict) -> str:
    rng = "-".join(_money(x) for x in v.amount_range) if v.amount_range else ""
    return f"{v.verdict}|{_money(v.amount_usd) or ''}|{rng}|{v.deadline or ''}"


def _previous_as_of(store: Store, mode: str, report_id: int) -> str | None:
    row = store.con.execute("SELECT as_of FROM reports WHERE mode = ? AND status = 'printed' AND id < ? "
                            "ORDER BY id DESC LIMIT 1", (mode, report_id)).fetchone()
    return row["as_of"] if row else None


def _retro(v: Verdict, headers: dict, prev_as_of: str | None) -> bool:
    if prev_as_of is None:
        return False
    cutoff = norm.end_of_day(prev_as_of)
    backdated = any(s.fact.kind in BACKDATED_KINDS and s.fact.known_at > cutoff and s.fact.valid_from
                    and day(s.fact.valid_from) <= day(prev_as_of) for s in v.evidence)
    if not backdated:
        return False
    if "invoice_id" in v.key:
        header = headers.get(v.key["invoice_id"])
        return header is not None and header.known_at <= cutoff
    return True


def _learned_at(terms: Terms, v: Verdict, action: str, base: Verdict | None, base_action: str | None) -> dict | None:
    """learned_at when feedback changed this key's answer, naming the decision that did."""
    if not terms.view.feedback:
        return None
    if base is not None and base.verdict in PRESENT and \
            (_signature(v), action) == (_signature(base), base_action):
        return None
    ids = list(v.feedback) + [it["item_id"] for it in reversed(terms.feedback.for_key(v.finding_key))]
    for item_id in ids:
        fb = next((it for it in terms.view.feedback if it["item_id"] == item_id), None)
        if fb is not None:
            return {"feedback_item_id": item_id, "batch": fb.get("after_batch") or ""}
    return None


def _citations(checker: Citations, v: Verdict) -> tuple[list[dict], int, int]:
    """-> (citations to print, each with its role, how many checked out, how many were dropped)."""
    kept: list[dict] = []
    seen: set[tuple] = set()
    dropped = 0

    def add(c: dict, src) -> None:
        key = (c["doc"], c["page"], norm_text(c["quote"]))
        if key not in seen:
            seen.add(key)
            kept.append({**c, "role": role(src), **({"detail": src.why} if src.why else {})})

    for s in v.evidence:
        c = checker.check(s)
        if c is None:
            dropped += 1
        else:
            add(c, s)
    good = len(kept)
    need = 2 if v.verdict == "conflict" else 1
    for s in v.evidence:   # too few checked out: keep the item valid with the reader's own citations (confidence
        if len(kept) >= need:   # and the action gate both know)
            break
        c = Citations.raw(s)
        if c is not None:
            add(c, s)
    return kept, good, dropped


def build_report(store: Store, as_of: str, mode: str, report_id: int) -> dict:
    view = View.load(store, as_of)
    terms = Terms(view)
    verdicts = adjudicate(view, terms)
    checker = Citations(store)
    cites = {v.finding_key: _citations(checker, v) for v in verdicts if v.verdict in PRESENT}
    cited = {k: c[1] for k, c in cites.items()}
    gates = policy.decide(terms, verdicts, mode, cited)
    base, base_gates = {}, {}
    if view.feedback:   # the same report with nobody's feedback, to tell what feedback changed
        plain = view.without_feedback()
        plain_terms = Terms(plain)
        plain_verdicts = adjudicate(plain, plain_terms)
        base = {v.finding_key: v for v in plain_verdicts}
        base_gates = policy.decide(plain_terms, plain_verdicts, mode, cited)
    headers = invoice_headers(view)
    prev_as_of = _previous_as_of(store, mode, report_id)

    rep = empty_report(as_of, mode)
    emitted = []
    for v in sorted((v for v in verdicts if v.verdict in PRESENT), key=lambda v: (v.cls, _key_str(v))):
        evidence, good, dropped = cites[v.finding_key]
        if len(evidence) < (2 if v.verdict == "conflict" else 1):
            log.warning("%s left out: no citation to print", v.finding_key)
            continue
        gate = gates[v.finding_key]
        base_id, sig = f"{v.cls}:{_key_str(v)}", _signature(v)
        prev = store.last_emitted(mode, v.finding_key, before_report=report_id)
        if prev is None:
            version, item_id, supersedes = 1, base_id, []
        elif prev["signature"] == sig:
            version, item_id = prev["version"], prev["item_id"]
            supersedes = loads(prev["item"]).get("supersedes") or []
        else:
            version = prev["version"] + 1
            item_id, supersedes = f"{base_id}~{version}", [prev["item_id"]]
        item = {"id": item_id, "class": v.cls, **v.key, "verdict": v.verdict,
                "amount_usd": _money(v.amount_usd) if v.verdict == "finding" else None,
                "amount_range": [_money(x) for x in v.amount_range] if v.verdict == "conflict" else None,
                "deadline": v.deadline, "action": gate.action,
                "confidence": confidence.score(terms, v, dropped=dropped, kept=good),
                "evidence": evidence, "missing_doc": v.missing_doc, "supersedes": supersedes,
                "learned_at": _learned_at(terms, v, gate.action, base.get(v.finding_key),
                                          base_gates[v.finding_key].action if v.finding_key in base_gates else None)}
        if prev is None and _retro(v, headers, prev_as_of):
            item["retro"] = True
        if gate.recipient and gate.action not in ("none", "escalate") and not gate.action.startswith("internal:"):
            item["recipient"] = {k: gate.recipient[k] for k in ("person", "role", "party", "email")
                                 if gate.recipient.get(k)}
        item["explanation"] = explanation(v)
        target = rep["findings"] if v.cls in INVOICE_CLASSES else rep["portfolio"][PORTFOLIO[v.cls]]
        target.append(item)
        emitted.append((v, gate, item, version, sig))

    problems = schema_errors(rep)
    if problems:
        raise ValueError(f"the report doesn't match the schema: {problems[0]}")
    for v, gate, item, version, sig in emitted:
        store.record_item(report_id, finding_key=v.finding_key, item_id=item["id"], version=version, signature=sig,
                          verdict=v.verdict, action=item["action"], item=item)
    from ..clerk.drafts import file_actions
    file_actions(store, report_id, mode, [(v, gate, item) for v, gate, item, _, _ in emitted])
    return rep
