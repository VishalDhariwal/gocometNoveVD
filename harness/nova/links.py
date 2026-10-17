"""Identifiers we already hold, found in a piece of text.

A reader sometimes leaves out an identifier that is plainly in the text it read: a late-fee line printed "Late fee on
TX-US-2035-0202 ..." with no referenced invoice, a rate notice whose reference "TBX-US-EXP-01/RN-..." names its
agreement. Code doesn't guess new identifiers; it only recognises ones the store already knows, and callers use a
match only when exactly one turns up.
"""
from __future__ import annotations

import re
from typing import Iterable


def find_known(text: str | None, known: Iterable[str]) -> list[str]:
    """The known identifiers that appear in `text` as whole tokens (not inside a longer code), in order of first
    appearance. Case and spacing don't matter ("tx-us-2035-0202", "TX-US-2035-0202")."""
    if not text:
        return []
    hay = re.sub(r"\s+", " ", str(text)).upper()
    hits = []
    for k in {str(k).upper() for k in known if k}:
        m = re.search(rf"(?<![A-Z0-9]){re.escape(k)}(?![A-Z0-9])", hay)
        if m:
            hits.append((m.start(), k))
    return [k for _, k in sorted(hits)]
