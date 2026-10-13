"""Reading feedback decisions with their scope, the strict way.

A standing decision applies only when EVERY filter in its applies_to matches and the date sits inside valid_from to
valid_to. A one-off decision touches exactly its own finding key. Phase 6 adds the key-level effects (close, reject,
override); this module serves what the terms engine needs now: authority grants, accepted rates, precedence.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .. import normalize as norm
from .view import View, day


def _same(field: str, want: Any, have: Any) -> bool:
    if have is None:
        return False
    if field in ("contract_id", "party"):
        return norm.code(want) == norm.code(have)
    if field == "lane":
        return norm.lane(want) == norm.lane(have)
    if field == "invoice_id":
        return norm.invoice_id(want) == norm.invoice_id(have)
    if field == "person":
        return re.sub(r"[^a-z]", "", str(want).lower()) == re.sub(r"[^a-z]", "", str(have).lower())
    return str(want).strip().lower() == str(have).strip().lower()


class Feedback:
    def __init__(self, view: View):
        self.items = view.feedback

    def standing(self, fact_type: str, on: date | None, **context: Any) -> list[dict]:
        """Standing decisions of this fact type whose every applies_to filter matches `context` and whose dates
        cover `on`. Latest decision first."""
        out = []
        for it in self.items:
            if it.get("fact_type") != fact_type or it.get("scope_kind") != "standing":
                continue
            applies = it.get("applies_to") or {}
            if not applies or not all(_same(k, v, context.get(k)) for k, v in applies.items()):
                continue
            start, end = day(it.get("valid_from")), day(it.get("valid_to"))
            if on is not None and ((start and on < start) or (end and on > end)):
                continue
            out.append(it)
        return sorted(out, key=lambda it: (it["decided_at"], it["id"]), reverse=True)

    def grant(self, *, contract: str, person: str | None, category: str, on: date | None) -> dict | None:
        """A confirmed sender_authority for this person, contract and category, in force on `on`."""
        for it in self.standing("sender_authority", on, contract_id=contract, person=person, category=category):
            if it.get("decision") == "confirm":
                return it
        return None

    def accepted_rates(self, contract: str, lane: str, on: date) -> list[dict]:
        return [it for it in self.standing("rate_accepted", on, contract_id=contract, lane=lane)
                if it.get("decision") in ("close", "confirm")]

    def precedence(self, contract: str, lane: str, on: date) -> dict | None:
        found = [it for it in self.standing("precedence", on, contract_id=contract, lane=lane)
                 if it.get("decision") == "resolve_conflict"]
        return found[0] if found else None
