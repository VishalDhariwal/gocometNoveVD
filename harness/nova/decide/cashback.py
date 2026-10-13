"""Cashback owed: earned under the contract and not yet credited (GLOSSARY "Cashback and credits").

- rebate: each closed quarter's Qualifying Spend (QS) at the tier it reaches, the rate applied to all of it.
- promised_credit: the annual improvement credit, a % of the prior contract year's QS, keyed CY1, CY2 ...
- margin_step_down: once a quarter's spend crosses the threshold, the handling margin on every later invoice that
  quarter should have been at the lower rate; the difference, summed per quarter.

QS follows the contract's definition (rules.qualifying_spend, read by Claude): the kinds of charges it includes, on
invoices dated in the period, net of credit notes dated in the period (rebate and improvement credits excepted).
Billed amounts, not corrected ones: a line billed twice counts twice until a credit note reverses it. INR invoices are
converted one by one at the month of their invoice date. Labels outside Annex E are left out, and noted.
A period is reported only once it has closed. Every credit note of that type and period comes off, whenever issued.
Due date passed: claim it; not yet due: track it.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from ..knowledge.terms import Decided, Missing, Source, Terms
from ..knowledge.view import dec, day
from ..money import pct_of, to_cents
from .common import (ZERO, Verdict, add_years, amount_of, contract_of, credit_notes, invoice_date, lines_of,
                     missing_doc, quarter_bounds, quarter_of, to_usd)

PART_CODES = {"base_freight": {"FRT"}, "fuel": {"FUEL"}, "security": {"SEC"}, "handling_margin": {"MARGIN"},
              "carrier_cost": {"COST"}, "duties": {"DUTY"}, "late_fees": {"LATE"}, "taxes": {"TAX"}}
DEFAULT_PARTS = ["base_freight", "fuel", "security", "annex_d_accessorials"]
NOT_SPEND = {"rebate", "promised_credit"}


class Spend:
    """Qualifying Spend over one period for one contract, in USD."""

    def __init__(self, terms: Terms, contract: str, headers: dict):
        self.terms, self.contract = terms, contract
        rule = terms.rule(contract, "qualifying_spend")
        self.rule = rule if isinstance(rule, Decided) else None
        parts = (self.rule.value.get("included") if self.rule else None) or DEFAULT_PARTS
        self.codes = set().union(*(PART_CODES.get(p, set()) for p in parts))
        if "annex_d_accessorials" in parts:
            self.codes |= {str(a.payload.get("code")) for a in terms.view.get("accessorial", contract_id=contract)
                           if a.payload.get("code")}
        self.with_tax = "taxes" in parts
        self.netting = (self.rule.value.get("credit_notes") if self.rule else None) or "net_all_dated_in_period"
        self.invoices = [h for h in headers.values() if contract_of(terms, h) == contract]

    def invoice(self, h) -> tuple[Decimal | None, object, Decimal]:
        """One invoice's qualifying amount -> (USD or None, the fx answer, amount left out for unknown labels)."""
        native, unknown = ZERO, ZERO
        for f in lines_of(self.terms.view, h):
            code = self.terms.charge_code(self.contract, f.payload.get("label"))
            if code is None:
                unknown += amount_of(f)
            elif code in self.codes:
                native += amount_of(f)
        if self.with_tax:
            native += sum((dec(t.get("amount")) or ZERO for t in h.payload.get("taxes") or []), ZERO)
        usd, fx = to_usd(self.terms, self.contract, h.payload.get("currency"), native, invoice_date(h))
        return usd, fx, unknown

    def over(self, start: date, end: date):
        """-> (QS in USD, invoices counted, notes, Missing if an exchange rate is missing)."""
        total, used, notes = ZERO, [], []
        in_period = [h for h in self.invoices if (d := invoice_date(h)) and start <= d <= end]
        for h in in_period:
            usd, fx, unknown = self.invoice(h)
            if unknown:
                notes.append(f"{h.invoice_id}: {unknown} on labels outside Annex E left out")
            if usd is None:
                return None, used, notes, fx if isinstance(fx, Missing) else None
            total += usd
            used.append((h, usd))
        if self.netting != "not_netted":
            ids = {h.invoice_id for h in in_period}
            for c in credit_notes(self.terms.view, self.contract):
                d = day(c.payload.get("document_date"))
                if c.payload.get("credit_class") in NOT_SPEND or not d or not start <= d <= end:
                    continue
                refs = set(c.payload.get("refers_to_invoices") or []) | ({c.invoice_id} if c.invoice_id else set())
                if self.netting == "net_only_for_period_invoices" and not refs & ids:
                    continue
                usd, fx = to_usd(self.terms, self.contract, c.payload.get("currency"), amount_of(c).copy_abs(), d)
                if usd is None:
                    return None, used, notes, fx if isinstance(fx, Missing) else None
                total -= usd
                notes.append(f"credit note {c.payload.get('number_as_printed')} {d}: -{usd}")
        return total, used, notes, None


def _credited(terms: Terms, contract: str, credit_class: str, period: str) -> list:
    out = []
    for c in credit_notes(terms.view, contract):
        if c.payload.get("credit_class") != credit_class:
            continue
        if _period_key(c.payload.get("period")) == period:
            out.append(c)
    return out


def _period_key(raw) -> str | None:
    """A credit note's period as printed -> '2033-Q2' or 'CY1'."""
    s = str(raw or "").upper()
    if m := re.search(r"(\d{4})\s*[-/ ]?\s*Q([1-4])", s) or re.search(r"Q([1-4])\s*[-/ ]?\s*(\d{4})", s):
        year, q = (m[1], m[2]) if len(m[1]) == 4 else (m[2], m[1])
        return f"{year}-Q{q}"
    if m := re.search(r"(?:CY|CONTRACT\s*YEAR)\s*(\d+)", s):
        return f"CY{int(m[1])}"
    return None


def _finish(terms: Terms, v: Verdict, earned: Decimal, credited: list, due: date | None) -> Verdict:
    as_of = terms.view.as_of_date
    paid = sum((to_usd(terms, v.contract, c.payload.get("currency"), amount_of(c).copy_abs(),
                       day(c.payload.get("document_date")))[0] or ZERO for c in credited), ZERO)
    owed = to_cents(earned - paid)
    v.steps.append(f"earned {earned}, credited {paid}, owed {owed}")
    for c in credited:
        v.cite(Source(c, 3, "credit already issued"))
    v.deadline = due.isoformat() if due else None
    if owed <= 0:
        v.verdict, v.why = "no_finding", "already credited"
        return v
    v.verdict, v.amount_usd, v.amount_native = "finding", owed, owed
    v.remedy = "claim_credit" if due is None or as_of > due else "track_credit"
    v.why = "earned and not credited" + ("" if v.remedy == "claim_credit" else f"; not due until {due}")
    return v


def detect(terms: Terms, headers: dict) -> list[Verdict]:
    out = []
    for contract in terms.view.contracts():
        spend = Spend(terms, contract, headers)
        if not spend.invoices:
            continue
        out += _rebates(terms, contract, spend)
        out += _improvement(terms, contract, spend)
        out += _margin(terms, contract, spend)
    return out


def _closed_quarters(terms: Terms, spend: Spend) -> list[str]:
    as_of = terms.view.as_of_date
    quarters = {quarter_of(d) for h in spend.invoices if (d := invoice_date(h))}
    return sorted(q for q in quarters if as_of and as_of > quarter_bounds(q)[1])


def _rebates(terms: Terms, contract: str, spend: Spend) -> list[Verdict]:
    rule = terms.rule(contract, "volume_rebate")
    if not isinstance(rule, Decided) or not rule.value.get("applicable") or rule.value.get("period") != "quarter":
        return []
    tiers = [(dec(t["threshold"]), dec(t["rate_pct"])) for t in rule.value.get("tiers") or []]
    out = []
    for q in _closed_quarters(terms, spend):
        start, end = quarter_bounds(q)
        v = Verdict("cashback", {"contract_id": contract, "period": q, "credit_type": "rebate"}, "no_finding",
                    contract=contract, currency="USD")
        qs, used, notes, missing = spend.over(start, end)
        v.steps += notes
        if missing is not None:
            v.verdict, v.remedy, v.missing_doc, v.why = "cannot_determine", "request_document", missing_doc(missing), \
                missing.why
            out.append(v)
            continue
        rate = max((r for t, r in tiers if qs >= t), default=None)
        v.steps.append(f"QS {q} = {qs} from {len(used)} invoice(s); tier rate {rate}")
        v.cite(rule.sources[0], spend.rule.sources[0] if spend.rule else None,
               *(Source(h, 3, "invoice in the quarter") for h, _ in sorted(used, key=lambda x: -x[1])[:3]))
        if rate is None:
            v.why = f"QS {qs} is below the first tier"
            out.append(v)
            continue
        due_days = rule.value.get("credit_due_days")
        due = end + timedelta(days=due_days) if isinstance(due_days, int) else None
        out.append(_finish(terms, v, pct_of(qs, rate), _credited(terms, contract, "rebate", q), due))
    return out


def _improvement(terms: Terms, contract: str, spend: Spend) -> list[Verdict]:
    rule = terms.rule(contract, "improvement_credit")
    c = next(iter(terms.view.get("contract", contract_id=contract)), None)
    eff = day(c.payload.get("effective_from")) if c else None
    if not isinstance(rule, Decided) or not rule.value.get("applicable") or eff is None:
        return []
    as_of, out, n = terms.view.as_of_date, [], 1
    while as_of and add_years(eff, n) - timedelta(days=1) < as_of:
        start, end = add_years(eff, n - 1), add_years(eff, n) - timedelta(days=1)
        v = Verdict("cashback", {"contract_id": contract, "period": f"CY{n}", "credit_type": "promised_credit"},
                    "no_finding", contract=contract, currency="USD")
        qs, used, notes, missing = spend.over(start, end)
        v.steps += notes
        if missing is not None:
            v.verdict, v.remedy, v.missing_doc, v.why = "cannot_determine", "request_document", missing_doc(missing), \
                missing.why
        elif qs > 0:
            v.steps.append(f"QS CY{n} ({start} to {end}) = {qs}")
            v.cite(rule.sources[0], spend.rule.sources[0] if spend.rule else None,
                   *(Source(h, 3, "invoice in the contract year") for h, _ in sorted(used, key=lambda x: -x[1])[:3]))
            due_days = rule.value.get("due_days_after_anniversary")
            due = end + timedelta(days=1 + due_days) if isinstance(due_days, int) else None
            v = _finish(terms, v, pct_of(qs, dec(rule.value["rate_pct"])),
                        _credited(terms, contract, "promised_credit", f"CY{n}"), due)
        out.append(v)
        n += 1
    return out


def _margin(terms: Terms, contract: str, spend: Spend) -> list[Verdict]:
    rule = terms.rule(contract, "handling_margin")
    if not isinstance(rule, Decided):
        return []
    r = rule.value
    threshold, new_rate = dec(r.get("step_down_threshold")), dec(r.get("step_down_rate_pct"))
    if not (r.get("applicable") and r.get("step_down_applicable") and threshold is not None and new_rate is not None):
        return []
    out = []
    for q in _closed_quarters(terms, spend):
        start, end = quarter_bounds(q)
        invoices = sorted((h for h in spend.invoices if start <= invoice_date(h) <= end),
                          key=lambda h: (invoice_date(h), h.known_at, h.id))
        running, crossed, over, cited = ZERO, False, ZERO, []
        for h in invoices:
            if crossed:
                for f in lines_of(terms.view, h):
                    if terms.charge_code(contract, f.payload.get("label")) != "MARGIN" or not f.payload.get("rate_is_pct"):
                        continue
                    base = dec(f.payload.get("quantity"))
                    if base is None:
                        continue
                    diff = amount_of(f) - pct_of(base, new_rate)
                    if diff > 0:
                        usd, _ = to_usd(terms, contract, h.payload.get("currency"), diff, invoice_date(h))
                        over += usd or ZERO
                        cited.append(f)
            if r.get("step_down_measure") == "quarter_billed_total":
                running += dec(h.payload.get("total")) or ZERO
            else:
                running += spend.invoice(h)[0] or ZERO
            crossed = crossed or running >= threshold
        if not cited:
            continue
        v = Verdict("cashback", {"contract_id": contract, "period": q, "credit_type": "margin_step_down"},
                    "no_finding", contract=contract, currency="USD")
        v.cite(rule.sources[0], *(Source(f, 3, "margin at the old rate") for f in cited[:3]))
        out.append(_finish(terms, v, to_cents(over), _credited(terms, contract, "margin_step_down", q),
                           end))
    return out
