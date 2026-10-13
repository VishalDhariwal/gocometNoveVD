"""Run the readers over every stored document that still needs reading. Called by `ingest` after intake.

- What needs reading: documents whose pages are all read and that have no current extraction, or whose current
  extraction came from an older reader version (a prompt change means a fresh read), or whose last read failed
  (up to config.MAX_READ_ATTEMPTS times).
- Routing: emails -> correspondence; spreadsheets -> tables; PDFs and images are classified first, then go to
  contract / billing / records / correspondence by type. Contracts go first: everything else is judged against them.
- Parallel: API calls run in a thread pool; only the main thread writes to the store, one transaction per document.
- Budget: no new read starts after the time budget; whatever is left is read by the next ingest.
- No API access: the first LLMUnavailable stops the round without recording failures, so nothing counts against the
  documents; they're simply read next time.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from ..llm import Gateway, LLMFailed, LLMUnavailable
from ..store import Store, StoreError
from .base import DocContext, ReadOutcome
from .billing import BillingReader
from .classify import VERSION as CLASSIFY_VERSION
from .classify import classify
from .contract import ContractReader
from .correspondence import CorrespondenceReader
from .records import RecordsReader
from .tables import TablesReader

log = logging.getLogger(__name__)

READERS = {r.name: r for r in (ContractReader(), CorrespondenceReader(), TablesReader(), BillingReader(),
                               RecordsReader())}
ROUTES = {"msa": "contract", "amendment": "contract", "annex_update": "contract",
          "invoice": "billing", "statement": "billing", "credit_note": "billing",
          "awb": "records", "master_awb": "records", "dg_declaration": "records", "customs_notice": "records",
          "rate_notice": "correspondence", "letter": "correspondence"}
PRIORITY = {"contract": 0, "correspondence": 1, "tables": 2, "billing": 3, "records": 4}


def reader_for(fmt: str, doc_type: str | None) -> str | None:
    if fmt == "eml":
        return "correspondence"
    if fmt == "xlsx":
        return "tables"
    return ROUTES.get(doc_type or "")


def _context(store: Store, row) -> DocContext:
    pages = [(p["page_no"], p["text"], p["source"]) for p in store.pages(row["id"])]
    email = parent = None
    if row["format"] == "eml":
        e = store.email(row["id"])
        if e is not None:
            email = dict(e)
            email["to_addrs"] = json.loads(email["to_addrs"])
    elif row["parent_id"]:
        e = store.email(row["parent_id"])
        parent = dict(e) if e is not None else None
    blob = store.blob(row["sha256"]) if row["format"] == "xlsx" else None
    return DocContext(row["id"], row["doc_ref"], row["format"], row["filename"], row["received_at"], pages,
                      row["doc_type"], email, parent, blob)


def _pending(store: Store) -> list:
    rows = store.con.execute(
        "SELECT d.*, e.reader AS cur_reader, e.reader_version AS cur_version, "
        "(SELECT COUNT(*) FROM extractions x WHERE x.doc_id = d.id AND x.status = 'failed') AS failures "
        "FROM documents d LEFT JOIN extractions e ON e.id = d.current_extraction_id "
        "WHERE d.status IN ('read', 'extracted', 'failed') ORDER BY d.id").fetchall()
    todo = []
    for row in rows:
        if row["cur_version"] is None:
            if row["failures"] >= config.MAX_READ_ATTEMPTS:
                continue
            todo.append(row)
        else:
            reader = READERS.get(row["cur_reader"])
            if reader is not None and row["cur_version"] != reader.version:
                todo.append(row)
    return todo


def extract_pending(store: Store, gateway: Gateway, *, run_id: int | None = None,
                    budget_s: float | None = None, workers: int | None = None) -> dict:
    deadline = time.monotonic() + (config.INGEST_BUDGET_S if budget_s is None else budget_s)
    stop = threading.Event()
    summary: Counter = Counter()
    todo = _pending(store)
    if not todo:
        return {}

    def guarded(fn, *args):
        if stop.is_set():
            return "unavailable"
        if time.monotonic() > deadline:
            return None
        try:
            return fn(*args)
        except LLMUnavailable as e:
            if not stop.is_set():
                log.warning("no API access, reading stops for this run: %s", e)
            stop.set()
            return "unavailable"

    with ThreadPoolExecutor(max_workers=workers or config.READER_WORKERS) as pool:
        # 1. classify PDFs and images that haven't been classified yet
        need_type = [row for row in todo if row["format"] in ("pdf", "other") and row["doc_type"] is None]
        contexts = {row["id"]: _context(store, row) for row in todo}

        def do_classify(ctx: DocContext):
            outcome = ReadOutcome("classify", CLASSIFY_VERSION)
            try:
                return classify(ctx, gateway, outcome)
            except LLMFailed as e:
                log.warning("%s: couldn't classify: %s", ctx.doc_ref, e)
                return None

        futures = {pool.submit(guarded, do_classify, contexts[row["id"]]): row for row in need_type}
        for fut in as_completed(futures):
            row, result = futures[fut], fut.result()
            if isinstance(result, tuple):
                store.update_document(row["id"], doc_type=result[0], doc_type_confidence=result[1])
                contexts[row["id"]].doc_type = result[0]
                summary["classified"] += 1

        # 2. read, contracts first
        jobs = []
        for row in todo:
            ctx = contexts[row["id"]]
            name = reader_for(row["format"], ctx.doc_type)
            if name is None:
                if ctx.doc_type is not None:  # classified as something we don't read (e.g. 'other')
                    store.update_document(row["id"], status="skipped")
                    summary["skipped"] += 1
                continue
            jobs.append((PRIORITY[name], row["id"], name, ctx))
        jobs.sort(key=lambda j: (j[0], j[1]))

        def do_read(name: str, ctx: DocContext) -> ReadOutcome:
            reader = READERS[name]
            try:
                return reader.read(ctx, gateway)
            except LLMFailed as e:
                return ReadOutcome(reader.name, reader.version, status="failed", error=str(e))
            except LLMUnavailable:
                raise
            except Exception as e:  # noqa: BLE001 - a reader bug fails one document, not the batch
                log.exception("%s: %s reader crashed", ctx.doc_ref, name)
                return ReadOutcome(reader.name, reader.version, status="failed", error=f"{type(e).__name__}: {e}")

        futures = {pool.submit(guarded, do_read, name, ctx): ctx for _, _, name, ctx in jobs}
        for fut in as_completed(futures):
            ctx, outcome = futures[fut], fut.result()
            if outcome is None:
                summary["left for next run"] += 1
            elif outcome == "unavailable":
                summary["left for next run (no API)"] += 1
            else:
                summary[_write(store, ctx, outcome, run_id)] += 1
                summary["facts"] += len(outcome.facts)
    return dict(summary)


def _write(store: Store, ctx: DocContext, outcome: ReadOutcome, run_id: int | None) -> str:
    """One transaction per document: the extraction, its facts, its warnings. -> 'read' / 'read with warnings' /
    'failed'."""
    with store.transaction():
        ext = store.begin_extraction(ctx.doc_id, outcome.reader, outcome.version, outcome.model)
        dropped = 0
        for f in outcome.facts:
            try:
                store.add_fact(ext, f.kind, f.payload, verification=f.verification, page=f.page, quote=f.quote,
                               valid_from=f.valid_from, valid_to=f.valid_to, **f.keys)
            except StoreError as e:
                dropped += 1
                outcome.warnings.append(f"a {f.kind} fact was dropped: {e}")
        status = outcome.status
        if status == "ok" and dropped:
            status = "partial"
        if status != "failed" and outcome.facts and dropped == len(outcome.facts):
            status = "failed"
        note = outcome.error or ("; ".join(outcome.warnings)[:4000] if outcome.warnings else None)
        store.finish_extraction(ext, status, model=outcome.model, error=note, input_tokens=outcome.input_tokens,
                                output_tokens=outcome.output_tokens)
        for w in ([outcome.error] if outcome.error else []) + outcome.warnings[:25]:
            store.log(run_id, "warning", f"{ctx.doc_ref}: {w}", doc_id=ctx.doc_id)
        if outcome.doc_type and not ctx.doc_type:
            store.update_document(ctx.doc_id, doc_type=outcome.doc_type[:60])
    return {"ok": "read", "partial": "read with warnings"}.get(status, "failed")
