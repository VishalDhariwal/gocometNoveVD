"""What the grader's three commands do. harness/harness.py parses the arguments and calls these.

Each command records a row in the store's `runs` table (what it did, what it cost). Diagnostics go to stderr; only
`report` writes to stdout, and only the report JSON.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .llm import Gateway
from .store import Store, StoreError

log = logging.getLogger("nova")


def _logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")


def ingest(batch_dir: str, state_dir: str) -> int:
    import time

    from . import config
    from .intake.mailbox import ingest_batch
    from .knowledge.reconcile import reconcile_labels
    from .readers.run import extract_pending

    _logging()
    started = time.monotonic()
    with Store.open(state_dir) as store:
        run_id = store.begin_run("ingest", {"batch_dir": str(batch_dir)})
        gateway = Gateway(store.cache)
        status, error = "ok", None
        try:
            summary = ingest_batch(store, Path(batch_dir), gateway, run_id=run_id)
            log.info("ingest %s: %s", Path(batch_dir).name, summary)
            deadline = config.step_deadline(started)
            read = extract_pending(store, gateway, run_id=run_id,
                                   budget_s=None if deadline is None else deadline - time.monotonic())
            log.info("read %s: %s", Path(batch_dir).name, read)
            labels = reconcile_labels(store, gateway)
            if labels.get("labels"):
                log.info("charge labels %s: %s", Path(batch_dir).name, labels)
        except Exception as e:  # noqa: BLE001 - recorded on the run, then re-raised for a non-zero exit
            status, error = "failed", f"{type(e).__name__}: {e}"
            raise
        finally:
            store.finish_run(run_id, status, error=error, api_calls=gateway.api_calls,
                             input_tokens=gateway.input_tokens, output_tokens=gateway.output_tokens)
    return 0


def _catch_up(store: Store, gateway: Gateway, run_id: int, deadline: float | None) -> None:
    """Read whatever ingest didn't get to (its clock ran out, or the API was away) before reporting, so no document
    waits for the next batch. The case reviewer keeps its own time: reading stops config.REVIEW_BUDGET_S before the
    step's deadline."""
    import time

    from . import config
    from .knowledge.reconcile import reconcile_labels
    from .readers.run import extract_pending

    if not config.report_reads_enabled():
        return
    budget = None if deadline is None else max(0.0, deadline - config.REVIEW_BUDGET_S - time.monotonic())
    try:
        read = extract_pending(store, gateway, run_id=run_id, budget_s=budget)
        if read:
            log.info("read before reporting: %s", read)
            reconcile_labels(store, gateway)
    except Exception:  # noqa: BLE001 - a report from what we have beats no report
        log.exception("reading leftovers before the report failed; reporting from what is read")


def report(as_of: str, mode: str, state_dir: str, gateway: Gateway | None = None) -> dict:
    """Never raises: if anything goes wrong, the error is logged and an empty, valid report is returned, because
    a report that parses scores more than a crash. API calls here: reading documents ingest didn't get to, and the
    case reviewer's (nova/review)."""
    import time

    from . import config
    from .report.build import build_report, empty_report, schema_errors

    started = time.monotonic()
    _logging()
    try:
        store = Store.open(state_dir)
    except Exception as e:  # noqa: BLE001
        log.error("can't open the state store in %s: %s", state_dir, e)
        return empty_report(as_of, mode)
    with store:
        run_id = store.begin_run("report", {"as_of": as_of, "mode": mode})
        report_id = store.begin_report(as_of, mode)
        gateway = gateway or Gateway(store.cache)     # the readers' and the case reviewer's door to Claude
        deadline = config.step_deadline(started)
        try:
            _catch_up(store, gateway, run_id, deadline)
            rep = build_report(store, as_of, mode, report_id, gateway=gateway, deadline=deadline)
            problems = schema_errors(rep)
            if problems:
                raise ValueError(f"the report doesn't match the schema: {problems[0]}")
        except Exception as e:  # noqa: BLE001
            log.exception("report failed; printing an empty report instead")
            rep = empty_report(as_of, mode)
            store.finish_report(report_id, rep, status="failed")
            store.finish_run(run_id, "failed", error=f"{type(e).__name__}: {e}", api_calls=gateway.api_calls,
                             input_tokens=gateway.input_tokens, output_tokens=gateway.output_tokens)
            return rep
        store.finish_report(report_id, rep)
        store.finish_run(run_id, "ok", api_calls=gateway.api_calls, input_tokens=gateway.input_tokens,
                         output_tokens=gateway.output_tokens)
        return rep


def feedback(feedback_file: str, state_dir: str) -> int:
    _logging()
    path = Path(feedback_file)
    with Store.open(state_dir) as store:
        run_id = store.begin_run("feedback", {"file": str(path)})
        try:
            file_id, new = store.add_feedback_file(str(path), path.read_bytes())
        except (OSError, StoreError) as e:
            log.error("feedback %s not taken in: %s", path, e)
            store.finish_run(run_id, "failed", error=str(e))
            return 1
        n = store.con.execute("SELECT COUNT(*) FROM feedback_items WHERE file_id = ?", (file_id,)).fetchone()[0]
        log.info("feedback %s: %s, %d decision(s)", path.name, "stored" if new else "already had it", n)
        store.finish_run(run_id, "ok")
    return 0
