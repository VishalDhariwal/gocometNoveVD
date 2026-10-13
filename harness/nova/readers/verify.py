"""Check what a reader says against the page it says it read.

- `locate`: find the reader's quote on a page (whitespace-insensitive, like the grader). If it isn't there, look for
  the real line that contains the item's key values and use that line instead ('repaired').
- `missing_values`: which of the item's printed values are not in its quote.
- `uncovered_lines`: lines that look like table rows (short, with a decimal number) that no extracted item quoted;
  a missed rate row or fuel month shows up here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence


def squash(s: object) -> str:
    return " ".join(str(s or "").split())


def nospace(s: object) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


@dataclass(frozen=True)
class Located:
    page: int
    quote: str
    how: str          # 'exact' (the reader's quote is on the page) or 'repaired' (we found the real line)


def locate(quote: str | None, pages: Sequence[tuple[int, str]], values: Iterable[object] = ()) -> Located | None:
    q = squash(quote)
    if len(nospace(q)) >= 6:
        for number, text in pages:
            if q in squash(text):
                return Located(number, q, "exact")
    tokens = [nospace(v) for v in values if v is not None and nospace(v)]
    if not tokens:
        return None
    best: tuple[float, int, str] | None = None
    for number, text in pages:
        for line in text.split("\n"):
            flat = nospace(line)
            if flat and all(t in flat for t in tokens):
                score = SequenceMatcher(None, nospace(q), flat).ratio() if q else 0.0
                if best is None or score > best[0]:
                    best = (score, number, squash(line))
    return Located(best[1], best[2], "repaired") if best else None


def missing_values(values: Iterable[object], quote: str | None) -> list[str]:
    flat = nospace(quote)
    return [str(v) for v in values if v is not None and nospace(v) and nospace(v) not in flat]


_DECIMAL = re.compile(r"\d\.\d")
_PAGE_FOOTER = re.compile(r"\bpage\s+\d+\s+of\s+\d+\b", re.IGNORECASE)


def table_like(line: str) -> bool:
    """Short lines carrying a decimal number: rate rows, fuel months, FX rows, fee rows."""
    s = squash(line)
    return 0 < len(s) <= 90 and bool(_DECIMAL.search(s)) and not _PAGE_FOOTER.search(s)


def uncovered_lines(pages: Sequence[tuple[int, str]], quotes: Iterable[str], want=table_like) -> list[str]:
    covered = [nospace(q) for q in quotes if q and len(nospace(q)) >= 6]
    blob = "\n".join(covered)
    out = []
    for _, text in pages:
        for line in text.split("\n"):
            if not want(line):
                continue
            flat = nospace(line)
            if flat in blob or any(len(c) >= 0.6 * len(flat) and c in flat for c in covered):
                continue
            out.append(squash(line))
    return out
