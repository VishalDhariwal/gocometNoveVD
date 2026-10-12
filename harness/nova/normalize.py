"""Canonical forms for everything we match on, in one place, so the store, the detectors and the report writer can
never disagree about what "the same invoice" or "the end of the as_of day" means."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone


def invoice_id(raw: object) -> str:
    """The glossary's rule: capitals, with spaces, '#' and ':' removed. Slashes and hyphens stay (TXI/35-36/0024)."""
    return re.sub(r"[\s#:]", "", str(raw)).upper()


def awb(raw: object) -> str:
    return re.sub(r"\s", "", str(raw)).upper()


def lane(raw: object) -> str:
    return re.sub(r"\s", "", str(raw)).upper()


def code(raw: object) -> str:
    """Contract ids, party codes, charge codes: capitals, no spaces."""
    return re.sub(r"\s", "", str(raw)).upper()


def email(raw: object) -> str:
    return str(raw).strip().lower()


def label(raw: object) -> str:
    """Charge labels compared without case, spaces or punctuation: scans and new layouts drop spaces ('Deliveryfee')."""
    return re.sub(r"[^0-9a-z]", "", str(raw).lower())


def month(d: date | str) -> str:
    """'YYYY-MM' for a date, the period key for fuel and FX tables."""
    return (d if isinstance(d, date) else date.fromisoformat(str(d)[:10])).strftime("%Y-%m")


def utc_iso(dt: datetime) -> str:
    """'YYYY-MM-DDTHH:MM:SSZ'. A naive datetime is refused: guessing a timezone shifts as_of by a day."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime: give it a timezone")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now() -> str:
    return utc_iso(datetime.now(timezone.utc))


def end_of_day(as_of: str) -> str:
    """The last second of the as_of day in UTC: documents received by then count (glossary: 'as_of')."""
    return f"{date.fromisoformat(as_of).isoformat()}T23:59:59Z"


def finding_key(d: dict) -> str:
    """'class|id...' for a report item, an answer file key or a feedback finding_key. Same fields as the grader's
    key_tuple, so our keys line up with theirs."""
    cls = d.get("class") or ("cashback" if "period" in d else "expiry")
    if cls == "cashback":
        return "|".join(("cashback", str(d.get("contract_id")), str(d.get("period")), str(d.get("credit_type"))))
    if cls == "expiry":
        return f"expiry|{d.get('contract_id')}"
    return f"{cls}|{invoice_id(d.get('invoice_id') or '')}"
