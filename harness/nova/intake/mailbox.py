"""`ingest` for one batch folder: every .eml and every attachment goes into the store with its page text.

Order of work for each email:
1. parse it and read every attachment's pages (this is where Claude vision may be called for a scan);
2. then write the receipt, the documents, the email fields and the pages in ONE transaction.
Reading happens before the transaction on purpose: a transaction never waits on the API, and a step killed mid-email
leaves no trace of that email, so the next run simply does it again.

The same file again is skipped (receipts are keyed by path), so running ingest twice is safe. At the end of every
batch, scanned pages that couldn't be read earlier (no API access at the time) are tried again.

Nothing is extracted into facts here; the readers do that in a later phase.
"""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

from ..llm import Gateway
from ..store import Store
from .mail import parse_email
from .pdf import PageText, read_pdf
from .sheets import xlsx_pages

log = logging.getLogger(__name__)

# When an email has no usable Date header. Visible to every report from now on, which is right: the email was in a
# batch delivered before the next report.
NO_DATE = "1970-01-01T00:00:00Z"

_IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "webp", "tif", "tiff", "bmp"}


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _page(number: int, text: str, source: str, **extra: Any) -> dict:
    return {"number": number, "text": text, "source": source, **extra}


def _pdf_pages(data: bytes, gateway: Gateway | None) -> tuple[list[dict], bool]:
    pages: list[PageText] = read_pdf(data, gateway)
    is_scan = bool(pages) and all(p.source != "text" for p in pages)
    return [vars(p).copy() for p in pages], is_scan


def read_attachment(name: str, data: bytes, gateway: Gateway | None) -> tuple[str, list[dict], bool]:
    """-> (format for the store, pages, is it a scan). Never raises: a file we can't read gets one 'unread' page."""
    ext = _ext(name)
    try:
        if ext == "pdf":
            pages, is_scan = _pdf_pages(data, gateway)
            return "pdf", pages, is_scan
        if ext in ("xlsx", "xlsm"):
            return "xlsx", [_page(i, t, "sheet") for i, t in enumerate(xlsx_pages(data), start=1)], False
        if ext in _IMAGE_TYPES:
            # A photographed or scanned page sent as an image: wrap it in a one-page PDF and read it like a scan.
            # The grader can't check quotes in image files, so the report must not cite them as evidence.
            import pymupdf
            with pymupdf.open(stream=data, filetype=ext) as img:
                pdf = img.convert_to_pdf()
            pages, _ = _pdf_pages(pdf, gateway)
            return "other", pages, True
        if ext in ("csv", "txt"):
            return "other", [_page(1, data.decode("utf-8", errors="replace"), "text")], False
    except Exception as e:  # noqa: BLE001 - one bad attachment must not cost us the rest of the email
        log.warning("%s: couldn't read (%s: %s)", name, type(e).__name__, e)
        return ("pdf" if ext == "pdf" else "xlsx" if ext in ("xlsx", "xlsm") else "other"), \
            [_page(1, "", "unread", error=f"{type(e).__name__}: {e}")], False
    return "other", [_page(1, "", "unread", error=f"unsupported attachment type .{ext or '?'}")], False


def _status(pages: list[dict]) -> tuple[str, str | None]:
    errors = [p.get("error") for p in pages if p["source"] == "unread"]
    return ("new", "; ".join(e for e in errors if e) or "unread page") if errors else ("read", None)


def ingest_email(store: Store, rel_path: str, batch: str, data: bytes, gateway: Gateway | None,
                 run_id: int | None = None) -> str:
    """-> 'new' or 'already had'."""
    if store.con.execute("SELECT 1 FROM receipts WHERE path = ?", (rel_path,)).fetchone():
        return "already had"
    em = parse_email(data)
    received_at = em.received_at or NO_DATE

    parts = []
    seen: set[str] = set()
    for name, payload in em.attachments:
        if name in seen:  # the grader's citation resolver takes the first attachment with a name
            em.warnings.append(f"a second attachment named {name!r} was skipped")
            continue
        seen.add(name)
        fmt, pages, is_scan = read_attachment(name, payload, gateway)
        parts.append((name, fmt, payload, pages, is_scan))

    with store.transaction():
        receipt_id, _ = store.add_receipt(path=rel_path, batch=batch, data=data, message_id=em.message_id,
                                          received_at=received_at)
        email_id, _ = store.add_document(doc_ref=rel_path, receipt_id=receipt_id, fmt="eml", data=data,
                                         received_at=received_at, filename=Path(rel_path).name)
        store.set_email(email_id, from_addr=em.from_addr, from_name=em.from_name, to_addrs=em.to_addrs,
                        cc_addrs=em.cc_addrs, subject=em.subject, message_id=em.message_id,
                        in_reply_to=em.in_reply_to, authored_text=em.authored, quoted_text=em.quoted)
        store.set_pages(email_id, [_page(1, em.page_text, "email")])
        store.update_document(email_id, status="read")
        for name, fmt, payload, pages, is_scan in parts:
            doc_id, _ = store.add_document(doc_ref=f"{rel_path}#{name}", receipt_id=receipt_id, parent_id=email_id,
                                           filename=name, fmt=fmt, data=payload, received_at=received_at,
                                           is_scan=is_scan)
            store.set_pages(doc_id, pages)
            status, error = _status(pages)
            store.update_document(doc_id, status=status, error=error)
        for w in em.warnings:
            store.log(run_id, "warning", f"{rel_path}: {w}", doc_id=email_id)
    return "new"


def retry_unread_pages(store: Store, gateway: Gateway | None, run_id: int | None = None) -> int:
    """Read again any PDF or image that still has an unread page (typically: a scan seen while the API was down).
    -> how many are now fully read."""
    if gateway is None:
        return 0
    rows = store.con.execute(
        "SELECT DISTINCT d.id, d.doc_ref, d.filename, d.sha256 FROM documents d JOIN pages p ON p.doc_id = d.id "
        "WHERE p.source = 'unread' AND d.format IN ('pdf', 'other')").fetchall()
    fixed = 0
    for row in rows:
        _, pages, is_scan = read_attachment(row["filename"] or "", store.blob(row["sha256"]), gateway)
        status, error = _status(pages)
        if status != "read":
            continue
        with store.transaction():
            store.set_pages(row["id"], pages)
            store.update_document(row["id"], status=status, error=error, is_scan=int(is_scan))
            store.log(run_id, "info", f"{row['doc_ref']}: read on retry")
        fixed += 1
    return fixed


def ingest_batch(store: Store, batch_dir: Path, gateway: Gateway | None, *, run_id: int | None = None) -> dict:
    """Ingest every .eml in `batch_dir` (a folder like <mailbox>/batch_03). -> counts of what happened."""
    batch_dir = Path(batch_dir)
    if not batch_dir.is_dir():
        raise FileNotFoundError(f"no batch folder {batch_dir}")
    batch = batch_dir.name
    summary: Counter = Counter()
    for path in sorted(p for p in batch_dir.iterdir() if p.is_file() and p.suffix.lower() == ".eml"):
        rel = f"{batch}/{path.name}"
        try:
            summary[ingest_email(store, rel, batch, path.read_bytes(), gateway, run_id)] += 1
        except Exception as e:  # noqa: BLE001 - one broken email must not stop the batch
            log.exception("%s: ingest failed", rel)
            store.log(run_id, "error", f"{rel}: ingest failed: {type(e).__name__}: {e}")
            summary["failed"] += 1
    summary["read on retry"] = retry_unread_pages(store, gateway, run_id)
    unread = store.con.execute("SELECT COUNT(DISTINCT doc_id) FROM pages WHERE source = 'unread'").fetchone()[0]
    if unread:
        summary["documents with unread pages"] = unread
    return dict(summary)
