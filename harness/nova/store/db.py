"""The state store: one SQLite file per state folder, <state-dir>/nova.db. The schema is in schema_v1.sql.

Why SQLite: it's in Python's standard library (nothing to install in the grader's container), it's a single file
inside --state-dir (the only place that survives between grader steps), and its transactions mean a step killed
halfway leaves the store exactly as it was before the document in flight.

Rules this module enforces:
- One connection per process, used from the main thread. Worker threads making API calls never touch it: they return
  results and the main thread writes. The LLM cache is the exception and opens its own connection per thread.
- Never hold a transaction open while waiting on the API. Transactions are short: one document, one report.
- Writes run inside `with store.transaction():` (BEGIN IMMEDIATE ... COMMIT, ROLLBACK on any exception). The write
  methods open one themselves; nested calls join the outer transaction.
- Source data is append-only (the schema's triggers refuse UPDATE and DELETE), facts are added only to a running
  extraction, and fact payloads may not contain floats.
- as_of filtering is a text comparison of UTC timestamps against the last second of that day.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Sequence

from .. import __version__
from .. import normalize as norm
from ..jsonx import dumps, loads
from .kinds import KEY_COLUMNS, KINDS

log = logging.getLogger(__name__)

DB_NAME = "nova.db"
# (version, file). Append new files; never edit one that has shipped.
MIGRATIONS: list[tuple[int, str]] = [(1, "schema_v1.sql")]
MIGRATIONS_LATEST = MIGRATIONS[-1][0]

_DOC_FIELDS = {"doc_type", "doc_type_confidence", "status", "error", "is_scan"}
_KEY_NORMALISERS = {"contract_id": norm.code, "invoice_id": norm.invoice_id, "awb": norm.awb, "lane": norm.lane,
                    "party": norm.code, "person_email": norm.email, "period": lambda v: str(v).strip()}


class StoreError(RuntimeError):
    pass


def _connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), isolation_level=None, timeout=30)  # autocommit; we issue BEGIN / COMMIT
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = DELETE")  # not WAL: its shared-memory file misbehaves on some mounted folders
    con.execute("PRAGMA synchronous = FULL")
    return con


def _no_floats(value: Any, where: str) -> None:
    if isinstance(value, float):
        raise StoreError(f"{where}: floats aren't allowed in facts; use Decimal or a decimal string for numbers")
    if isinstance(value, dict):
        for k, v in value.items():
            _no_floats(v, f"{where}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _no_floats(v, f"{where}[{i}]")


def _check_date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as e:
        raise StoreError(f"{name} must be YYYY-MM-DD, got {value!r}") from e


@dataclass(frozen=True)
class Fact:
    id: int
    kind: str
    payload: dict
    contract_id: str | None
    invoice_id: str | None
    awb: str | None
    lane: str | None
    party: str | None
    person_email: str | None
    period: str | None
    valid_from: str | None
    valid_to: str | None
    known_at: str
    doc_id: int
    doc_ref: str
    page: int | None
    quote: str | None
    verification: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Fact":
        d = dict(row)
        return cls(**{f: (loads(d[f]) if f == "payload" else d[f]) for f in cls.__dataclass_fields__})


class SqliteCache:
    """The API gateway's cache (nova.llm.Gateway), in the llm_cache table. Safe from worker threads: each thread gets
    its own connection to the same file and SQLite serialises the writes. A failed write is logged and dropped:
    a cache miss next time costs money, not correctness."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = _connect(self.path)
        return con

    def get(self, key: str) -> dict | None:
        row = self._con().execute("SELECT model, data, input_tokens, output_tokens FROM llm_cache WHERE key = ?",
                                  (key,)).fetchone()
        if row is None:
            return None
        return {"data": loads(row["data"]), "model": row["model"], "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"]}

    def put(self, key: str, value: dict) -> None:
        try:
            self._con().execute(
                "INSERT OR REPLACE INTO llm_cache(key, purpose, version, model, data, input_tokens, output_tokens, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, value.get("purpose", ""), value.get("version", ""), value["model"], dumps(value["data"]),
                 value.get("input_tokens", 0), value.get("output_tokens", 0), norm.utc_now()))
        except sqlite3.Error as e:
            log.warning("couldn't cache an API answer (%s); it will be asked again next time", e)


class Store:
    def __init__(self, con: sqlite3.Connection, path: Path):
        self.con = con
        self.path = path
        self._cache: SqliteCache | None = None

    # ---- opening ----------------------------------------------------------------------------------------------
    @classmethod
    def open(cls, state_dir: str | Path) -> "Store":
        """Open (or create) the store in `state_dir` and bring its schema up to date."""
        folder = Path(state_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / DB_NAME
        store = cls(_connect(path), path)
        try:
            store.con.execute("SELECT json_valid('{}')").fetchone()
        except sqlite3.OperationalError as e:
            store.close()
            raise StoreError("this SQLite build has no JSON functions (needs SQLite 3.38 or newer)") from e
        try:
            store._migrate()
        except Exception:
            store.close()
            raise
        return store

    def _migrate(self) -> None:
        current = self.schema_version
        latest = MIGRATIONS_LATEST
        if current > latest:
            raise StoreError(f"{self.path} is schema v{current}; this code only knows up to v{latest}. "
                             "Refusing to touch it.")
        for version, filename in MIGRATIONS:
            if version <= current:
                continue
            sql = (Path(__file__).parent / filename).read_text()
            try:
                self.con.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
            except Exception:
                if self.con.in_transaction:
                    self.con.execute("ROLLBACK")
                raise
            log.info("state store migrated to schema v%d", version)

    @property
    def schema_version(self) -> int:
        return self.con.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        if self.con.in_transaction:  # nested: join the outer transaction
            yield self
            return
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")

    @property
    def cache(self) -> SqliteCache:
        if self._cache is None:
            self._cache = SqliteCache(self.path)
        return self._cache

    # ---- what arrived -----------------------------------------------------------------------------------------
    def put_blob(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        with self.transaction():
            self.con.execute("INSERT OR IGNORE INTO blobs(sha256, size, data) VALUES (?, ?, ?)", (sha, len(data), data))
        return sha

    def blob(self, sha256: str) -> bytes:
        row = self.con.execute("SELECT data FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
        if row is None:
            raise StoreError(f"no blob {sha256}")
        return row["data"]

    def add_receipt(self, *, path: str, batch: str, data: bytes, message_id: str | None,
                    received_at: str) -> tuple[int, bool]:
        """-> (receipt id, is it new). The same path again is a no-op, so running ingest twice is safe."""
        sha = hashlib.sha256(data).hexdigest()
        with self.transaction():
            row = self.con.execute("SELECT id, sha256 FROM receipts WHERE path = ?", (path,)).fetchone()
            if row is not None:
                if row["sha256"] != sha:
                    raise StoreError(f"{path} was ingested before with different content")
                return row["id"], False
            self.put_blob(data)
            cur = self.con.execute(
                "INSERT INTO receipts(path, batch, sha256, message_id, received_at, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (path, batch, sha, message_id, received_at, norm.utc_now()))
            return cur.lastrowid, True

    def add_document(self, *, doc_ref: str, receipt_id: int, fmt: str, data: bytes, received_at: str,
                     filename: str | None = None, parent_id: int | None = None,
                     is_scan: bool = False) -> tuple[int, bool]:
        """-> (document id, is it new). Idempotent by doc_ref."""
        with self.transaction():
            row = self.con.execute("SELECT id FROM documents WHERE doc_ref = ?", (doc_ref,)).fetchone()
            if row is not None:
                return row["id"], False
            sha = self.put_blob(data)
            cur = self.con.execute(
                "INSERT INTO documents(doc_ref, receipt_id, parent_id, filename, format, sha256, received_at, is_scan) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_ref, receipt_id, parent_id, filename, fmt, sha, received_at, int(is_scan)))
            return cur.lastrowid, True

    def update_document(self, doc_id: int, **fields: Any) -> None:
        bad = set(fields) - _DOC_FIELDS
        if bad:
            raise StoreError(f"can't set {', '.join(sorted(bad))} on a document")
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            with self.transaction():
                self.con.execute(f"UPDATE documents SET {cols} WHERE id = ?", (*fields.values(), doc_id))

    def document(self, ref: int | str) -> sqlite3.Row | None:
        col = "id" if isinstance(ref, int) else "doc_ref"
        return self.con.execute(f"SELECT * FROM documents WHERE {col} = ?", (ref,)).fetchone()

    def documents(self, *, status: str | None = None) -> list[sqlite3.Row]:
        if status is None:
            return self.con.execute("SELECT * FROM documents ORDER BY id").fetchall()
        return self.con.execute("SELECT * FROM documents WHERE status = ? ORDER BY id", (status,)).fetchall()

    def set_email(self, doc_id: int, *, from_addr: str, authored_text: str, quoted_text: str = "",
                  from_name: str | None = None, to_addrs: Sequence[str] = (), cc_addrs: Sequence[str] = (),
                  subject: str | None = None, message_id: str | None = None, in_reply_to: str | None = None) -> None:
        with self.transaction():
            self.con.execute(
                "INSERT OR REPLACE INTO emails(doc_id, message_id, from_addr, from_name, to_addrs, cc_addrs, subject, "
                "in_reply_to, authored_text, quoted_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, message_id, norm.email(from_addr), from_name, dumps([norm.email(a) for a in to_addrs]),
                 dumps([norm.email(a) for a in cc_addrs]), subject, in_reply_to, authored_text, quoted_text))

    def email(self, doc_id: int) -> sqlite3.Row | None:
        return self.con.execute("SELECT * FROM emails WHERE doc_id = ?", (doc_id,)).fetchone()

    def set_pages(self, doc_id: int, pages: Sequence[Any]) -> None:
        """Replace a document's page text. Accepts dicts or nova.intake.pdf.PageText objects."""
        rows = []
        for p in pages:
            d = asdict(p) if is_dataclass(p) else dict(p)
            rows.append((doc_id, d.get("number") or d.get("page_no"), d["text"], d["source"], d.get("model"),
                         d.get("unreadable_marks", 0), d.get("ocr_agreement"), d.get("notes"), d.get("error")))
        with self.transaction():
            self.con.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
            self.con.executemany(
                "INSERT INTO pages(doc_id, page_no, text, source, model, unreadable_marks, ocr_agreement, notes, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def pages(self, doc_id: int) -> list[sqlite3.Row]:
        return self.con.execute("SELECT * FROM pages WHERE doc_id = ? ORDER BY page_no", (doc_id,)).fetchall()

    # ---- what we read from it ---------------------------------------------------------------------------------
    def begin_extraction(self, doc_id: int, reader: str, reader_version: str, model: str | None = None) -> int:
        with self.transaction():
            cur = self.con.execute(
                "INSERT INTO extractions(doc_id, reader, reader_version, model, started_at) VALUES (?, ?, ?, ?, ?)",
                (doc_id, reader, reader_version, model, norm.utc_now()))
            return cur.lastrowid

    def add_fact(self, extraction_id: int, kind: str, payload: dict, *, verification: str, page: int | None = None,
                 quote: str | None = None, valid_from: str | None = None, valid_to: str | None = None,
                 **keys: Any) -> int:
        """Store one fact from a running extraction. Keys are normalised; the kind's required keys must be there."""
        spec = KINDS.get(kind)
        if spec is None:
            raise StoreError(f"unknown fact kind {kind!r} (see nova/store/kinds.py)")
        unknown = set(keys) - set(KEY_COLUMNS)
        if unknown:
            raise StoreError(f"unknown key column(s) {', '.join(sorted(unknown))}")
        values = {c: (_KEY_NORMALISERS[c](keys[c]) or None) if keys.get(c) not in (None, "") else None
                  for c in KEY_COLUMNS}
        missing = [c for c in spec.keys if not values[c]]
        if missing:
            raise StoreError(f"a {kind} fact needs {', '.join(missing)}")
        if not isinstance(payload, dict):
            raise StoreError("a fact's payload must be a JSON object")
        _no_floats(payload, kind)
        valid_from, valid_to = _check_date(valid_from, "valid_from"), _check_date(valid_to, "valid_to")
        if valid_from and valid_to and valid_from > valid_to:
            raise StoreError(f"valid_from {valid_from} is after valid_to {valid_to}")
        ext = self.con.execute(
            "SELECT e.status, e.doc_id, d.received_at FROM extractions e JOIN documents d ON d.id = e.doc_id "
            "WHERE e.id = ?", (extraction_id,)).fetchone()
        if ext is None:
            raise StoreError(f"no extraction {extraction_id}")
        if ext["status"] != "running":
            raise StoreError(f"extraction {extraction_id} is {ext['status']}: facts can only be added while it runs")
        with self.transaction():
            cur = self.con.execute(
                f"INSERT INTO facts(kind, {', '.join(KEY_COLUMNS)}, payload, valid_from, valid_to, known_at, doc_id, "
                "extraction_id, page, quote, verification, created_at) "
                f"VALUES (?, {', '.join('?' * len(KEY_COLUMNS))}, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, *values.values(), dumps(payload), valid_from, valid_to, ext["received_at"], ext["doc_id"],
                 extraction_id, page, quote, verification, norm.utc_now()))
            return cur.lastrowid

    def finish_extraction(self, extraction_id: int, status: str, *, model: str | None = None,
                          error: str | None = None, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Close a reader pass. 'ok' or 'partial' makes it the document's current pass; 'failed' leaves the previous
        current pass (if any) in place."""
        if status not in ("ok", "partial", "failed"):
            raise StoreError(f"bad extraction status {status!r}")
        with self.transaction():
            ext = self.con.execute("SELECT doc_id, status FROM extractions WHERE id = ?", (extraction_id,)).fetchone()
            if ext is None or ext["status"] != "running":
                raise StoreError(f"extraction {extraction_id} isn't running")
            self.con.execute(
                "UPDATE extractions SET status = ?, finished_at = ?, model = COALESCE(?, model), error = ?, "
                "input_tokens = ?, output_tokens = ? WHERE id = ?",
                (status, norm.utc_now(), model, error, input_tokens, output_tokens, extraction_id))
            if status == "failed":
                self.con.execute(
                    "UPDATE documents SET error = ?, status = CASE WHEN current_extraction_id IS NULL THEN 'failed' "
                    "ELSE status END WHERE id = ?", (error, ext["doc_id"]))
            else:
                self.con.execute("UPDATE documents SET current_extraction_id = ?, status = 'extracted', error = NULL "
                                 "WHERE id = ?", (extraction_id, ext["doc_id"]))

    def facts(self, kind: str | None = None, *, as_of: str | None = None, **keys: Any) -> list[Fact]:
        """Current facts, oldest first. as_of keeps only facts whose document arrived by the end of that day."""
        where, args = [], []
        if kind is not None:
            where.append("kind = ?")
            args.append(kind)
        if as_of is not None:
            where.append("known_at <= ?")
            args.append(norm.end_of_day(as_of))
        for col, val in keys.items():
            if col not in KEY_COLUMNS:
                raise StoreError(f"unknown key column {col!r}")
            where.append(f"{col} = ?")
            args.append(_KEY_NORMALISERS[col](val))
        sql = "SELECT * FROM current_facts" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY known_at, id"
        return [Fact.from_row(r) for r in self.con.execute(sql, args)]

    # ---- links ------------------------------------------------------------------------------------------------
    def put_link(self, kind: str, subject: str, obj: str | None, *, method: str, confidence: float, known_at: str,
                 detail: dict | None = None) -> None:
        with self.transaction():
            self.con.execute(
                "INSERT INTO links(kind, subject, object, method, confidence, detail, known_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(kind, subject, known_at) DO UPDATE SET "
                "object = excluded.object, method = excluded.method, confidence = excluded.confidence, "
                "detail = excluded.detail, created_at = excluded.created_at",
                (kind, subject, obj, method, confidence, dumps(detail or {}), known_at, norm.utc_now()))

    def links(self, kind: str, *, as_of: str | None = None) -> dict[str, sqlite3.Row]:
        """subject -> the newest link of this kind whose inputs had all arrived by as_of."""
        cutoff = norm.end_of_day(as_of) if as_of else "9999-12-31T23:59:59Z"
        rows = self.con.execute("SELECT * FROM links WHERE kind = ? AND known_at <= ? ORDER BY known_at, id",
                                (kind, cutoff)).fetchall()
        return {r["subject"]: r for r in rows}  # later rows overwrite earlier ones

    # ---- what people decided ----------------------------------------------------------------------------------
    def add_feedback_file(self, path: str, data: bytes) -> tuple[int, bool]:
        """Store a feedback.json file and its items. -> (file id, is it new). The same bytes twice: no-op."""
        sha = hashlib.sha256(data).hexdigest()
        row = self.con.execute("SELECT id FROM feedback_files WHERE sha256 = ?", (sha,)).fetchone()
        if row is not None:
            return row["id"], False
        try:
            doc = loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise StoreError(f"{path} is not valid JSON: {e}") from e
        if not isinstance(doc, dict) or not doc.get("decided_at"):
            raise StoreError(f"{path} has no decided_at")
        with self.transaction():
            cur = self.con.execute(
                "INSERT INTO feedback_files(sha256, path, world, after_batch, decided_at, decided_by, raw, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sha, path, doc.get("world"), doc.get("after_batch"), doc["decided_at"],
                 dumps(doc.get("decided_by") or {}), dumps(doc), norm.utc_now()))
            file_id = cur.lastrowid
            for item in doc.get("items") or []:
                if not isinstance(item, dict) or not item.get("item_id"):
                    log.warning("%s: skipped a feedback item with no item_id", path)
                    continue
                fact = item.get("fact") or {}
                scope = item.get("scope") or {}
                self.con.execute(
                    "INSERT INTO feedback_items(file_id, item_id, finding_key, solicited, decision, fact_type, fact, "
                    "scope_kind, applies_to, valid_from, valid_to, decided_at, after_batch, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (file_id, str(item["item_id"]), norm.finding_key(item.get("finding_key") or {}),
                     None if item.get("solicited") is None else int(bool(item["solicited"])),
                     str(item.get("decision") or ""), fact.get("type"), dumps(fact), scope.get("kind"),
                     dumps(scope.get("applies_to") or {}), scope.get("valid_from"), scope.get("valid_to"),
                     doc["decided_at"], doc.get("after_batch"), item.get("note")))
            return file_id, True

    def feedback_items(self, *, as_of: str | None = None) -> list[dict]:
        """Decisions in the order they were made. as_of keeps those decided by the end of that day."""
        sql, args = "SELECT * FROM feedback_items", []
        if as_of is not None:
            sql += " WHERE decided_at <= ?"
            args.append(norm.end_of_day(as_of))
        out = []
        for r in self.con.execute(sql + " ORDER BY decided_at, id", args):
            d = dict(r)
            d["fact"], d["applies_to"] = loads(d["fact"]), loads(d["applies_to"])
            out.append(d)
        return out

    # ---- what we said and did ---------------------------------------------------------------------------------
    def begin_report(self, as_of: str, mode: str) -> int:
        norm.end_of_day(as_of)  # validates the date
        with self.transaction():
            return self.con.execute("INSERT INTO reports(as_of, mode, created_at) VALUES (?, ?, ?)",
                                    (as_of, mode, norm.utc_now())).lastrowid

    def record_item(self, report_id: int, *, finding_key: str, item_id: str, version: int, signature: str,
                    verdict: str, action: str, item: dict) -> None:
        with self.transaction():
            self.con.execute(
                "INSERT INTO emitted_items(report_id, finding_key, item_id, version, signature, verdict, action, item) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (report_id, finding_key, item_id, version, signature, verdict, action, dumps(item)))

    def finish_report(self, report_id: int, report: dict | None, *, status: str = "printed") -> None:
        with self.transaction():
            self.con.execute("UPDATE reports SET status = ?, report = ? WHERE id = ?",
                             (status, None if report is None else dumps(report), report_id))

    def previous_items(self, mode: str, *, before_report: int) -> dict[str, sqlite3.Row]:
        """finding_key -> what the last printed report before `before_report` said about it."""
        row = self.con.execute("SELECT id FROM reports WHERE mode = ? AND status = 'printed' AND id < ? "
                               "ORDER BY id DESC LIMIT 1", (mode, before_report)).fetchone()
        if row is None:
            return {}
        return {r["finding_key"]: r for r in
                self.con.execute("SELECT * FROM emitted_items WHERE report_id = ?", (row["id"],))}

    def last_emitted(self, mode: str, finding_key: str, *, before_report: int) -> sqlite3.Row | None:
        """The latest printed item for this key in any earlier report (for version numbers and supersedes)."""
        return self.con.execute(
            "SELECT e.* FROM emitted_items e JOIN reports r ON r.id = e.report_id WHERE r.mode = ? "
            "AND r.status = 'printed' AND e.finding_key = ? AND r.id < ? ORDER BY r.id DESC LIMIT 1",
            (mode, finding_key, before_report)).fetchone()

    def queue_outbound(self, *, idempotency_key: str, report_id: int, mode: str, action: str, finding_key: str,
                       item_id: str, subject: str, body: str, status: str, recipient_email: str | None = None,
                       recipient_name: str | None = None) -> bool:
        """-> True if queued now, False if this exact action was queued before (nothing goes out twice)."""
        with self.transaction():
            cur = self.con.execute(
                "INSERT OR IGNORE INTO outbox(idempotency_key, report_id, mode, action, finding_key, item_id, "
                "recipient_email, recipient_name, subject, body, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (idempotency_key, report_id, mode, action, finding_key, item_id,
                 norm.email(recipient_email) if recipient_email else None, recipient_name, subject, body, status,
                 norm.utc_now()))
            return cur.rowcount == 1

    # ---- runs -------------------------------------------------------------------------------------------------
    def begin_run(self, command: str, args: dict | None = None) -> int:
        with self.transaction():
            return self.con.execute("INSERT INTO runs(command, args, nova_version, started_at) VALUES (?, ?, ?, ?)",
                                    (command, dumps(args or {}), __version__, norm.utc_now())).lastrowid

    def finish_run(self, run_id: int, status: str, *, error: str | None = None, api_calls: int = 0,
                   input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self.transaction():
            self.con.execute(
                "UPDATE runs SET status = ?, finished_at = ?, error = ?, api_calls = ?, input_tokens = ?, "
                "output_tokens = ? WHERE id = ?",
                (status, norm.utc_now(), error, api_calls, input_tokens, output_tokens, run_id))

    def log(self, run_id: int | None, level: str, message: str, *, doc_id: int | None = None) -> None:
        with self.transaction():
            self.con.execute("INSERT INTO run_log(run_id, at, level, doc_id, message) VALUES (?, ?, ?, ?, ?)",
                             (run_id, norm.utc_now(), level, doc_id, message))
