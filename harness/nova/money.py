"""Money arithmetic, the glossary's way: Decimal everywhere, rounded half up to cents (ties away from zero), and
nothing rounded except where a rule says so."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def pct_of(base: Decimal, pct: Decimal) -> Decimal:
    """pct% of base, rounded to cents: pct_of(Decimal('1612.29'), Decimal('20.16')) == Decimal('325.04')."""
    return to_cents(base * pct / 100)
