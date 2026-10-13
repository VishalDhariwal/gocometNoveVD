"""Excel attachments: tracking exports, payment ledgers (AP registers), statements.

Claude looks at the first rows of each sheet once and says what the sheet is and which column holds what. Every row
is then parsed by code, straight from the cells: thousands of tracking rows never pass through the model, so none can
be misread or skipped by it. Each fact's quote is the row exactly as the grader prints it (cells joined by ' | ').

A statement laid out in a sheet is handed to the billing reader, page = that sheet, like any other bill.
"""
from __future__ import annotations

import io
import re

from .. import config
from .. import normalize as norm
from ..llm import Gateway
from . import values as v
from .base import COMMON_RULES, STR, DocContext, FactOut, ReadOutcome, call, tool
from .billing import BillingReader
from ..intake.sheets import SEP, _cell_text

VERSION = "tables-v2"
SAMPLE_ROWS = 15

SYSTEM = COMMON_RULES + """

This is one sheet of a spreadsheet the shipper received. You see its first rows; each cell is shown as [column number] 'text'. Say what kind of sheet it is and which column holds each field:
- tracking_export: shipment events (waybill, event, date, station, reason code)
- payment_ledger: the shipper's payable invoices (invoice number, invoice date, due date, amount, currency, status, paid date)
- statement: a carrier's statement of account or bill laid out in a sheet
- rate_table: a table of rates or fees
- other: anything else
header_row is the row number of the column headings. Leave out any field the sheet doesn't have. as_of: the date the sheet says it is current as of, if it says one.

After the rows you also see every distinct value of the sheet's short text columns. In value_meanings, map each distinct value of the event column to an event, each distinct value of the status column to a status, and each distinct reason code to a cause, by what the value means rather than how it is spelled. Leave a value out only if you can't tell what it means.

Statuses: paid (payment made), scheduled (approved for a payment run but not yet paid), open (unpaid and not scheduled), disputed (payment withheld because of a dispute), cancelled (void, or credited in full), other.

""" + v.EVENT_HELP + "\n\n" + v.CAUSE_HELP

COLUMN_FIELDS = ("awb", "event", "date", "station", "reason_code", "invoice", "invoice_date", "due_date", "amount",
                 "currency", "status", "paid_date")

TOOL = tool("record_sheet_layout", "Record what a sheet is and which column holds what.", {
    "table_type": {"enum": ["tracking_export", "payment_ledger", "statement", "rate_table", "other"]},
    "header_row": {"type": "integer"},
    "columns": {"type": "object", "properties": {f: {"type": "integer"} for f in COLUMN_FIELDS}},
    "as_of": STR,
    "value_meanings": {"type": "object", "properties": {
        "events": {"type": "array", "items": {"type": "object", "properties": {
            "printed": STR, "event": {"enum": v.EVENTS}}, "required": ["printed", "event"]}},
        "statuses": {"type": "array", "items": {"type": "object", "properties": {
            "printed": STR, "status": {"enum": v.LEDGER_STATUSES}}, "required": ["printed", "status"]}},
        "reason_codes": {"type": "array", "items": {"type": "object", "properties": {
            "printed": STR, "cause": {"enum": v.CAUSES}}, "required": ["printed", "cause"]}}}},
    "notes": STR,
}, ["table_type"])


def sheet_rows(data: bytes) -> list[tuple[str, list[tuple[int, list[str]]]]]:
    """[(sheet title, [(row number, cell texts with their positions kept)])], cell text as the grader shows it."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for r, row in enumerate(ws.iter_rows(), start=1):
            cells = [_cell_text(c) for c in row]
            if any(cells):
                rows.append((r, cells))
        out.append((ws.title, rows))
    return out


def grader_row(cells: list[str]) -> str:
    return SEP.join(c for c in cells if c != "")


def _cell(cells: list[str], col: int | None) -> str | None:
    if col is None or not 0 <= col < len(cells):
        return None
    return v.text_or_none(cells[col])


MAX_DISTINCT = 40


def distinct_values(rows: list[tuple[int, list[str]]]) -> str:
    """Every distinct value of each short text column (dates, numbers and free-text columns left out)."""
    cols: dict[int, set[str]] = {}
    for _, cells in rows:
        for i, c in enumerate(cells):
            if c and re.search(r"[A-Za-z]", c) and len(c) <= 60:
                cols.setdefault(i, set()).add(c.strip())
    lines = [f"[{i}] " + ", ".join(repr(x) for x in sorted(vals))
             for i, vals in sorted(cols.items()) if len(vals) <= MAX_DISTINCT]
    return "\n".join(lines)


def _meanings(layout: dict, key: str, field: str, allowed: list[str]) -> dict[str, str]:
    out = {}
    for m in (layout.get("value_meanings") or {}).get(key) or []:
        if isinstance(m, dict) and v.text_or_none(m.get("printed")) and m.get(field) in allowed:
            out[str(m["printed"]).strip()] = m[field]
    return out


def _canonical(raw: str | None, meanings: dict[str, str], allowed: list[str], default: str) -> str | None:
    """What Claude said the printed value means; a value already printed as one of the choices is taken as is."""
    if not raw:
        return None
    raw = raw.strip()
    if raw in meanings:
        return meanings[raw]
    for form in (raw, raw.upper().replace(" ", "_"), raw.lower()):
        if form in allowed:
            return form
    return default


class TablesReader:
    name = "tables"
    version = VERSION

    def read(self, ctx: DocContext, gateway: Gateway) -> ReadOutcome:
        out = ReadOutcome(self.name, self.version)
        sheets = sheet_rows(ctx.blob or b"")
        kinds = []
        for index, (title, rows) in enumerate(sheets, start=1):
            if not rows:
                continue
            sample = "\n".join(f"row {r}: " + " ".join(f"[{i}] {c!r}" for i, c in enumerate(cells) if c)
                               for r, cells in rows[:SAMPLE_ROWS])
            hints = f"(Context: file {ctx.filename!r}"
            if ctx.parent_email:
                hints += f", attached to an email with subject {ctx.parent_email.get('subject')!r}"
            content = (f"{hints}; sheet {index} {title!r}, {len(rows)} non-empty rows.)\n<sheet>\n{sample}\n</sheet>\n"
                       f"<distinct_values>\n{distinct_values(rows)}\n</distinct_values>")
            layout = call(gateway, out, purpose="sheet_layout", models=config.CLASSIFY_MODELS, system=SYSTEM,
                          tool_def=TOOL, content=content, max_tokens=800)
            kind = layout.get("table_type") or "other"
            kinds.append(kind)
            if kind == "statement":
                page = next(((n, t, s) for n, t, s in ctx.pages if n == index), None)
                sub = DocContext(ctx.doc_id, ctx.doc_ref, ctx.format, ctx.filename, ctx.received_at,
                                 [page] if page else [], ctx.doc_type, ctx.email, ctx.parent_email)
                billed = BillingReader().read(sub, gateway)
                out.facts += billed.facts
                out.warnings += billed.warnings
                out.input_tokens += billed.input_tokens
                out.output_tokens += billed.output_tokens
            elif kind in ("tracking_export", "payment_ledger"):
                facts, problems = parse_rows(kind, index, rows, layout, ctx)
                out.facts += facts
                out.warnings += problems
            else:
                out.warnings.append(f"sheet {index} ({title!r}) is a {kind}; nothing read from it")
        out.doc_type = "+".join(sorted(set(kinds))) or None
        out.status = "partial" if any(w for w in out.warnings if "nothing read" not in w) else "ok"
        return out


def parse_rows(kind: str, page: int, rows: list[tuple[int, list[str]]], layout: dict,
               ctx: DocContext) -> tuple[list[FactOut], list[str]]:
    cols = {k: val for k, val in (layout.get("columns") or {}).items() if isinstance(val, int)}
    header = layout.get("header_row")
    body = [(r, cells) for r, cells in rows if header is None or r > header]
    problems: list[str] = []
    facts: list[FactOut] = []
    date_cols = [c for c in ("date", "invoice_date", "due_date", "paid_date") if c in cols]
    order = v.date_order([_cell(cells, cols[c]) for _, cells in body for c in date_cols])
    as_of = v.iso(v.parse_date(layout.get("as_of"), order))

    need = ("awb", "event", "date") if kind == "tracking_export" else ("invoice", "amount", "status")
    events = _meanings(layout, "events", "event", v.EVENTS)
    statuses = _meanings(layout, "statuses", "status", v.LEDGER_STATUSES)
    causes = _meanings(layout, "reason_codes", "cause", v.CAUSES)
    missing = [c for c in need if c not in cols]
    if missing:
        return [], [f"sheet {page}: no column for {', '.join(missing)}; rows not read"]

    for r, cells in body:
        quote = grader_row(cells)
        if kind == "tracking_export":
            awb, event, raw_date = _cell(cells, cols["awb"]), _cell(cells, cols["event"]), _cell(cells, cols["date"])
            d = v.parse_date(raw_date, order)
            if not awb or not event or d is None:
                problems.append(f"sheet {page} row {r} skipped: {quote!r}")
                continue
            reason = _cell(cells, cols.get("reason_code"))
            facts.append(FactOut("tracking_event", {
                "event": _canonical(event, events, v.EVENTS, "OTHER"), "event_as_printed": event,
                "date": d.isoformat(), "station": _cell(cells, cols.get("station")), "reason_code": reason,
                "cause": causes.get(reason.strip()) if reason else None},
                keys={"awb": awb}, page=page, quote=quote, verification="structured",
                valid_from=d.isoformat(), valid_to=d.isoformat()))
        else:
            inv, raw_amount = _cell(cells, cols["invoice"]), _cell(cells, cols["amount"])
            amount = v.parse_amount(raw_amount)
            if not inv or amount is None:
                problems.append(f"sheet {page} row {r} skipped: {quote!r}")
                continue
            currency = _cell(cells, cols.get("currency"))
            if not currency and raw_amount:
                m = re.search(r"\b([A-Z]{3})\b", raw_amount)
                currency = m.group(1) if m else None
            facts.append(FactOut("ledger_entry", {
                "invoice_number_as_printed": inv,
                "invoice_date": v.iso(v.parse_date(_cell(cells, cols.get("invoice_date")), order)),
                "due_date": v.iso(v.parse_date(_cell(cells, cols.get("due_date")), order)),
                "amount": amount, "currency": currency,
                "status": _canonical(_cell(cells, cols.get("status")), statuses, v.LEDGER_STATUSES, "other"),
                "status_as_printed": _cell(cells, cols.get("status")),
                "paid_date": v.iso(v.parse_date(_cell(cells, cols.get("paid_date")), order)),
                "register_as_of": as_of, "register_received_at": ctx.received_at},
                keys={"invoice_id": inv}, page=page, quote=quote, verification="structured"))
    return facts, problems
