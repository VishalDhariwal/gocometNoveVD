"""Turn values copied "as printed" into typed values, deterministically.

Readers ask Claude to copy numbers and dates exactly as they appear ("1,701.31", "03/06/2030", "19.41%"). This module
does every conversion, the same way every time, so a layout change can't make the model silently reformat a value.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

_CURRENCY = re.compile(r"(?<![A-Za-z])(USD|INR|EUR|GBP|US\$|Rs\.?)|[$€£₹]", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,.]*")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_MONTH_NAME = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"

# ---- fixed choices Claude answers with (the prompts define each one) ----
EVENTS = ["PICKED_UP", "DEPARTED", "ARRIVED_FACILITY", "OUT_FOR_DELIVERY", "DELIVERED", "COLLECTED_BY_CONSIGNEE",
          "CUSTOMS_HOLD", "RELEASED", "OTHER"]
CAUSES = ["carrier_documents", "shipper_or_consignee", "customs_or_other", "not_stated"]
LEDGER_STATUSES = ["paid", "scheduled", "open", "disputed", "cancelled", "other"]
LINE_UNITS = ["kg", "shipment", "piece", "day", "month", "invoice", "percent_of_base", "other"]
COPY_MARKERS = ["copy", "duplicate", "reminder", "for_your_records", "reissued", "pro_forma", "other"]
BASIS_UNITS = ["per_kg", "per_shipment", "per_day", "per_month", "per_invoice", "percent", "other"]

EVENT_HELP = """Events, by what happened (not by the words used):
- PICKED_UP: the carrier took possession from the shipper (picked up, collected from shipper, tendered, accepted)
- DEPARTED: left an origin or transit point, or the flight departed
- ARRIVED_FACILITY: arrived at the destination warehouse or facility (where storage time starts)
- OUT_FOR_DELIVERY: on its way to the consignee
- DELIVERED: the carrier delivered it to the consignee's address
- COLLECTED_BY_CONSIGNEE: the consignee collected it from the carrier's facility, so the carrier did not deliver
- CUSTOMS_HOLD: held or examined by customs
- RELEASED: released by customs
- OTHER: anything else"""

CAUSE_HELP = """Cause of a customs hold, from the reason given:
- carrier_documents: an error or omission in documents the carrier prepared or filed (its waybill, manifest, filing)
- shipper_or_consignee: the shipper's or consignee's documents, permits, licences, goods, duties or payments
- customs_or_other: a routine or random examination, or any other cause that isn't either party's documents
- not_stated: no reason given, or it isn't clear whose fault it was"""

UNITS = {"kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "shpt": "shipment", "shpts": "shipment",
         "shipment": "shipment", "shipments": "shipment", "day": "day", "days": "day", "month": "month",
         "months": "month", "mo": "month", "pc": "piece", "pcs": "piece", "piece": "piece", "pieces": "piece"}


def parse_amount(raw: object) -> Decimal | None:
    """'1,701.31' -> 1701.31; 'INR127,024.44' -> 127024.44; '(12.50)' or '-12.50' -> -12.50; '1.701,31' -> 1701.31.
    None when there's no number."""
    if raw is None:
        return None
    s = _CURRENCY.sub(" ", str(raw)).strip()
    m = _NUMBER.search(s)
    if not m:
        return None
    negative = (s.startswith("(") and s.endswith(")")) or s[:m.start()].rstrip().endswith(("-", "−"))
    tok = m.group().rstrip(".,")
    if "," in tok and "." in tok:
        tok = tok.replace(".", "").replace(",", ".") if tok.rfind(",") > tok.rfind(".") else tok.replace(",", "")
    elif "," in tok:
        tail = tok.rsplit(",", 1)[1]
        tok = tok.replace(",", "") if len(tail) == 3 else tok.replace(",", ".")
    elif tok.count(".") > 1:
        tok = tok.replace(".", "")
    try:
        value = Decimal(tok)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_pct(raw: object) -> Decimal | None:
    """'19.41%' -> 19.41 (a percentage, not a fraction)."""
    return parse_amount(raw)


def is_pct(raw: object) -> bool:
    return raw is not None and "%" in str(raw)


def parse_quantity(raw: object) -> tuple[Decimal | None, str | None]:
    """'456 kg' -> (456, 'kg'); '1 shpt' -> (1, 'shipment'); '274kg' -> (274, 'kg'); '1,194.72' -> (1194.72, None)."""
    if raw is None:
        return None, None
    s = str(raw).strip()
    value = parse_amount(s)
    m = re.search(r"\d[\d,.]*\s*([A-Za-z]+)", s)
    unit = UNITS.get(m.group(1).lower(), m.group(1).lower()) if m else None
    return value, unit


def _ymd(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")
_ISO_DATE = re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")


def date_order(raws: list[object]) -> str | None:
    """'dmy' or 'mdy' when the numeric dates in a document settle it (a first part above 12 means day-first), else
    None. A document uses one format throughout, so all its dates are looked at together."""
    dmy = mdy = False
    for raw in raws:
        for a, b, _ in _NUMERIC_DATE.findall(str(raw or "")):
            dmy |= int(a) > 12
            mdy |= int(b) > 12
    if dmy and not mdy:
        return "dmy"
    if mdy and not dmy:
        return "mdy"
    return None


def parse_date(raw: object, order: str | None = None) -> date | None:
    """ISO '2035-01-16'; '16/01/2035' (needs `order` when both parts are 12 or less); '14 May 2033'; '12-Mar-2031';
    'May 14, 2033'. None when it can't be read without guessing."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if m := _ISO_DATE.search(s):
        return _ymd(int(m[1]), int(m[2]), int(m[3]))
    if m := re.search(rf"\b(\d{{1,2}})[\s\-/.]*{_MONTH_NAME}[\s\-/.,]*(\d{{4}})\b", s):
        return _ymd(int(m[3]), _MONTHS[m[2][:3]], int(m[1]))
    if m := re.search(rf"\b{_MONTH_NAME}\s+(\d{{1,2}}),?\s+(\d{{4}})\b", s):
        return _ymd(int(m[3]), _MONTHS[m[1][:3]], int(m[2]))
    if m := _NUMERIC_DATE.search(s):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if order is None:
            order = "dmy" if a > 12 else "mdy" if b > 12 else None
        if order == "dmy":
            return _ymd(y, b, a)
        if order == "mdy":
            return _ymd(y, a, b)
    return None


def iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def parse_month(raw: object) -> str | None:
    """A calendar month as 'YYYY-MM': '2034-10', 'Oct 2034', 'October 2034', '10/2034'."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if m := re.search(r"\b(\d{4})[-/.](\d{1,2})\b", s):
        if 1 <= int(m[2]) <= 12:
            return f"{int(m[1]):04d}-{int(m[2]):02d}"
    if m := re.search(rf"\b{_MONTH_NAME}\s*[-/]?\s*(\d{{4}})\b", s):
        return f"{int(m[2]):04d}-{_MONTHS[m[1][:3]]:02d}"
    if m := re.search(r"\b(\d{1,2})[-/.](\d{4})\b", s):
        if 1 <= int(m[1]) <= 12:
            return f"{int(m[2]):04d}-{int(m[1]):02d}"
    return None


def parse_band(raw: object) -> tuple[str, Decimal | None, Decimal | None]:
    """A weight band: 'minimum charge' -> ('minimum', None, None); '0-44.5 kg' -> ('per_kg', 0, 44.5);
    '300+ kg' -> ('per_kg', 300, None); 'up to 44.5 kg' -> ('per_kg', 0, 44.5). ('unknown', None, None) otherwise."""
    s = str(raw or "").lower().replace("–", "-")
    if "min" in s:
        return "minimum", None, None
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", s):
        return "per_kg", Decimal(m[1]), Decimal(m[2])
    if m := re.search(r"(\d+(?:\.\d+)?)\s*\+|(?:over|above|from)\s*(\d+(?:\.\d+)?)", s):
        return "per_kg", Decimal(m[1] or m[2]), None
    if m := re.search(r"(?:up to|under|below|<=?)\s*(\d+(?:\.\d+)?)", s):
        return "per_kg", Decimal("0"), Decimal(m[1])
    return "unknown", None, None


def band_of(row: dict) -> tuple[str, Decimal | None, Decimal | None, str | None]:
    """A rate row's weight band from what Claude read (band_kind, band_from, band_to), checked against the band as
    printed. Falls back to parse_band when Claude left the fields out or its numbers aren't in the printed band.
    -> (kind, lower, upper, problem or None)."""
    printed = str(row.get("band") or "")
    kind = row.get("band_kind")
    if kind == "minimum_charge":
        return "minimum", None, None, None
    if kind == "per_kg":
        lo, hi = parse_amount(row.get("band_from")), parse_amount(row.get("band_to"))
        digits = re.sub(r"[^0-9.]", " ", printed).split()
        shown = {Decimal(d) for d in digits if re.fullmatch(r"\d+(?:\.\d+)?", d)}
        if (lo is None or lo == 0 or lo in shown) and (hi is None or hi in shown) and (lo is not None or hi is not None):
            return "per_kg", lo if lo is not None else Decimal("0"), hi, None
        fallback = parse_band(printed)
        return (*fallback, f"band {printed!r}: read as {row.get('band_from')}-{row.get('band_to')}, which isn't what's "
                           f"printed; used the printed band")
    return (*parse_band(printed), None)


def months_list(raw: object) -> list[int] | None:
    """Months of the year (1-12) Claude listed for a seasonal fee; None when it listed none or any isn't a month."""
    if not isinstance(raw, list) or not raw:
        return None
    try:
        months = sorted({int(m) for m in raw})
    except (TypeError, ValueError):
        return None
    return months if all(1 <= m <= 12 for m in months) else None


def choice(raw: object, allowed: list[str], default: str | None = None) -> str | None:
    """A fixed-choice answer, or `default` when it isn't one of the choices."""
    return raw if isinstance(raw, str) and raw in allowed else default


def text_or_none(raw: object) -> str | None:
    s = str(raw).strip() if raw is not None else ""
    return s if s and s not in ("-", "—", "n/a", "N/A", "null", "None") else None
