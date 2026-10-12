"""JSON that keeps money exact: Decimals are written as plain decimal strings ("1701.31"), dates as ISO text.
Keys are sorted, so the same data always serialises to the same bytes."""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"can't store {type(o).__name__} as JSON")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_default, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def loads(text: str) -> Any:
    return json.loads(text)
