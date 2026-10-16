"""Every citation is re-checked before it's printed, the way the grader checks it (grader/extract.py `found` and
`Mailbox.check`): the quote, whitespace-normalised, must be on the cited page of the cited document. A scanned PDF
has no text layer, so there only the page number is checked. A quote found on another page is moved there; one found
nowhere is dropped. Quotes under 12 non-space characters don't count as citations, so they're dropped too.
"""
from __future__ import annotations

import re

from ..knowledge.terms import Source
from ..store import Store

MIN_QUOTE = 12


def norm(s: str) -> str:
    return " ".join(s.split())


class Citations:
    def __init__(self, store: Store):
        self.store = store
        self._docs: dict[str, tuple[dict[int, str], bool] | None] = {}

    def _pages(self, doc_ref: str):
        if doc_ref not in self._docs:
            row = self.store.document(doc_ref)
            if row is None:
                self._docs[doc_ref] = None
            else:
                pages = {p["page_no"]: p["text"] or "" for p in self.store.pages(row["id"])}
                self._docs[doc_ref] = (pages, bool(row["is_scan"])) if pages else None
        return self._docs[doc_ref]

    def check(self, src: Source) -> dict | None:
        """-> {doc, page, quote} if the grader will find it, else None."""
        f = src.fact
        quote, page = (f.quote or "").strip(), f.page or 1
        if len(re.sub(r"\s", "", quote)) < MIN_QUOTE or not f.doc_ref:
            return None
        held = self._pages(f.doc_ref)
        if held is None:
            return None
        pages, scan = held
        if scan:
            return {"doc": f.doc_ref, "page": page, "quote": quote} if page in pages else None
        q = norm(quote)
        if q in norm(pages.get(page, "")):
            return {"doc": f.doc_ref, "page": page, "quote": quote}
        for n in sorted(pages):
            if q in norm(pages[n]):
                return {"doc": f.doc_ref, "page": n, "quote": quote}
        return None

    @staticmethod
    def raw(src: Source) -> dict | None:
        """The citation as the reader gave it, unchecked (only used when nothing checked out, to keep the item)."""
        f = src.fact
        return {"doc": f.doc_ref, "page": f.page or 1, "quote": f.quote.strip()} if f.quote and f.doc_ref else None
