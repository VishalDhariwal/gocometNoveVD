"""The reviewer, start to finish, for one report: triage, the agents in parallel, then code's checks in a fixed order.

The checks run one case at a time in triage order, in the report's own thread, so the result doesn't depend on which
agent finished first, and a line one case takes can't be taken again by the next.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .. import config
from ..decide.common import Verdict
from ..knowledge.terms import Terms
from ..llm import Gateway, LLMUnavailable
from ..store import Store
from . import agent, check, triage
from .library import Library

log = logging.getLogger(__name__)


def review(store: Store, gateway: Gateway | None, terms: Terms, verdicts: list[Verdict], *, mode: str, gates: dict,
           report_id: int | None = None, enabled: bool | None = None) -> list[Verdict]:
    """-> the verdicts, with what the reviewer added or held. Never raises: any failure leaves code's verdicts."""
    if gateway is None or not (config.review_enabled() if enabled is None else enabled):
        return verdicts
    try:
        return _review(store, gateway, terms, verdicts, mode, gates, report_id)
    except Exception:  # noqa: BLE001 - the reviewer is a second look; the report must not depend on it
        log.exception("the case reviewer failed; keeping code's verdicts")
        return verdicts


def _review(store, gateway, terms, verdicts, mode, gates, report_id) -> list[Verdict]:
    lib = Library(store, terms)
    cases = triage.pick(lib, verdicts, gates, mode, config.REVIEW_MAX_CASES)
    if not cases:
        return verdicts
    try:
        gateway.client   # no key: say so once instead of once per case
    except LLMUnavailable as e:
        log.warning("case reviewer skipped (%d case(s)): %s", len(cases), e)
        return verdicts
    claimed = {line: v.cls for v in verdicts if v.verdict == "finding" for line in v.lines}
    deadline = time.monotonic() + config.REVIEW_BUDGET_S
    with ThreadPoolExecutor(max_workers=max(1, config.REVIEW_WORKERS)) as pool:
        futures = [pool.submit(agent.run_case, gateway, lib, c, dict(claimed), deadline) for c in cases]
        results = []
        for fut in futures:
            try:
                results.append(fut.result())
            except LLMUnavailable as e:
                results.append(agent.Result(None, error=f"no API access: {e}"))
            except Exception as e:  # noqa: BLE001
                log.exception("a reviewer case failed")
                results.append(agent.Result(None, error=f"{type(e).__name__}: {e}"))

    by_key = {v.finding_key: v for v in verdicts}
    order = [v.finding_key for v in verdicts]
    counts: dict[str, int] = {}
    for case, res in zip(cases, results):
        if res.opinion is None:
            outcome = check.Outcome("failed", res.error or "no opinion")
        else:
            outcome = check.judge(lib, case, res.opinion, by_key, claimed)
        if outcome.kind == "upgraded" and outcome.verdict is not None:
            v = outcome.verdict
            if v.finding_key not in by_key:
                order.append(v.finding_key)
            by_key[v.finding_key] = v
            for line in v.lines:
                claimed.setdefault(line, v.cls)
        counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
        log.info("review %s (%s): %s - %s", case.key, case.reason, outcome.kind, outcome.detail)
        if report_id is not None:
            store.add_review(report_id, finding_key=case.key, reason=case.reason, opinion=res.opinion,
                             outcome=outcome.kind, detail=outcome.detail, model=res.model, turns=res.turns)
    log.info("case reviewer: %d case(s), %s", len(cases), ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    return [by_key[k] for k in order]
