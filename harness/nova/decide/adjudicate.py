"""Every finding key, one verdict each, for one as_of date.

Order matters because of the claim ledger: duplicates, then waivers, then late fees not owed, then overcharges, so a
billed line is only ever claimed once. Late-fee risk comes after the invoice classes because what we're disputing on
an invoice comes off its principal. Cashback and expiry are independent.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from ..knowledge.pricing import price_invoice
from ..knowledge.terms import Terms
from ..knowledge.view import View, day
from . import cashback, duplicate, expiry, late_fees, overcharge, waiver
from .common import ZERO, Claims, Verdict, invoice_headers

log = logging.getLogger(__name__)


def adjudicate(view: View) -> list[Verdict]:
    if view.as_of_date is None:   # no date given: as of the newest document we hold
        newest = max((d.received_at for d in view.docs.values()), default=None)
        view.as_of_date = day(newest)
    terms = Terms(view)
    claims = Claims()
    headers = invoice_headers(view)
    priced = {inv_id: price_invoice(terms, h) for inv_id, h in headers.items()}

    out: list[Verdict] = []
    out += duplicate.detect(terms, claims, headers)
    out += waiver.detect(terms, claims, headers)
    out += late_fees.not_owed(terms, claims, headers)
    out += overcharge.detect(terms, claims, priced)
    disputed: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for v in out:
        if v.verdict == "finding" and v.remedy == "dispute" and v.amount_native is not None:
            disputed[v.key["invoice_id"]] += v.amount_native
    out += late_fees.risks(terms, headers, disputed)
    out += cashback.detect(terms, headers)
    out += expiry.detect(terms)

    seen: dict[str, Verdict] = {}
    for v in out:
        if v.finding_key in seen:
            log.warning("two verdicts for %s; keeping the first", v.finding_key)
            continue
        seen[v.finding_key] = v
    return list(seen.values())
