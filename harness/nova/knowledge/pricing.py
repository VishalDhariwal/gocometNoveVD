"""What each billed line should have cost, line by line, the glossary's way.

- Lines are grouped by waybill. A shipment is priced on its pickup date, taken from the first of: the tracking
  export's PICKED_UP, a carrier "picked up" email, the waybill's executed date, the date printed on the invoice.
- Freight is the band rate for the printed chargeable weight (the waybill's weight is only a cross-check), never below
  the lane's minimum. Fuel is the pickup month's % of the correct freight. Fees come from Annex D and their clauses.
- Money is Decimal, each line rounded half up to cents; a % is taken of already-rounded lines.
- When the freight rate is in conflict, every line that depends on it gets one price per option (a scenario); the
  invoice then has a range. When it depends on a document we don't hold, the line is `missing`.
- What we can't price from records is `unpriceable` and what isn't ours to price here (tax, duty, late fees, margin,
  pass-through cost, labels outside Annex E) is `not_priced`. Neither is ever a finding.

Undercharges are kept on the line (billed below expected) but `over()` only counts what was billed above.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .. import normalize as norm
from ..money import pct_of, to_cents
from ..store import Fact
from .terms import Conflict, Decided, Missing, Result, Source, Terms, Undefined
from .view import dec, day, domain

FREIGHT, FUEL, TAX = "FRT", "FUEL", "TAX"
NOT_PRICED = {"TAX", "DUTY", "LATE", "MARGIN", "COST"}
RELEASE_EVENTS = ("DELIVERED", "COLLECTED_BY_CONSIGNEE")
ZERO = Decimal("0")


@dataclass
class Pickup:
    date: date | None
    source: str | None          # tracking | status_email | awb | invoice
    fact: Fact | None
    notes: list[str] = field(default_factory=list)


@dataclass
class LinePrice:
    line: Fact
    code: str | None
    billed: Decimal
    status: str = "pending"     # priced | conflict | missing | unpriceable | not_priced
    expected: Decimal | None = None
    options: dict[str, Decimal] = field(default_factory=dict)   # conflict: scenario label -> expected
    missing: Missing | None = None
    why: str = ""
    steps: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)

    @property
    def feedback(self) -> list[str]:
        return sorted({s.feedback for s in self.sources if s.feedback})

    def expected_in(self, scenario: str | None = None) -> Decimal | None:
        if self.status == "priced":
            return self.expected
        if self.status == "conflict" and scenario is not None:
            return self.options.get(scenario)
        return None

    def over(self, scenario: str | None = None) -> Decimal:
        """Billed above expected (0 when billed at or below it, or when we can't say)."""
        e = self.expected_in(scenario)
        return max(ZERO, self.billed - e) if e is not None else ZERO

    def cite(self, src: Source | None) -> None:
        if src is not None and all(s.fact.id != src.fact.id for s in self.sources):
            self.sources.append(src)

    def set(self, status: str, why: str = "", expected: Decimal | None = None) -> "LinePrice":
        self.status, self.why = status, why
        if expected is not None:
            self.expected = to_cents(expected)
        return self


@dataclass
class ShipmentPricing:
    awb: str | None
    lane: str | None
    pickup: Pickup
    weight: Decimal | None
    lines: list[LinePrice]
    scenarios: list[str] = field(default_factory=list)
    conflict: Conflict | None = None
    notes: list[str] = field(default_factory=list)

    def overcharge(self, scenario: str | None = None) -> Decimal:
        return sum((lp.over(scenario) for lp in self.lines), ZERO)

    def overcharge_range(self) -> tuple[Decimal, Decimal]:
        if not self.scenarios:
            v = self.overcharge()
            return v, v
        vals = [self.overcharge(s) for s in self.scenarios]
        return min(vals), max(vals)


@dataclass
class TaxCheck:
    billed: Decimal
    expected: Decimal | None
    status: str                 # priced | not_priced
    why: str
    sources: list[Source] = field(default_factory=list)

    def over(self) -> Decimal:
        return max(ZERO, self.billed - self.expected) if self.status == "priced" and self.expected is not None else ZERO


@dataclass
class InvoicePricing:
    invoice: Fact
    contract: str | None
    currency: str | None
    shipments: list[ShipmentPricing]
    other: list[LinePrice]      # lines with no waybill: monthly fees, adjustments
    tax: TaxCheck | None
    fx: Result | None
    notes: list[str] = field(default_factory=list)

    @property
    def invoice_id(self) -> str:
        return self.invoice.invoice_id

    def lines(self) -> list[LinePrice]:
        return [lp for s in self.shipments for lp in s.lines] + self.other

    def missing(self) -> list[Missing]:
        out = [lp.missing for lp in self.lines() if lp.status == "missing" and lp.missing]
        return out + ([self.fx] if isinstance(self.fx, Missing) else [])

    def conflicts(self) -> list[LinePrice]:
        return [lp for lp in self.lines() if lp.status == "conflict"]

    def overcharge_range(self) -> tuple[Decimal, Decimal]:
        """(lowest, highest) amount billed above the correct price, in the invoice's currency."""
        lo = hi = sum((lp.over() for lp in self.other), ZERO) + (self.tax.over() if self.tax else ZERO)
        for s in self.shipments:
            a, b = s.overcharge_range()
            lo, hi = lo + a, hi + b
        return lo, hi


# ---- finding things -------------------------------------------------------------------------------------------
def contract_for(terms: Terms, invoice: Fact, lines: list[Fact]) -> tuple[str | None, str | None]:
    held = set(terms.view.contracts())
    if invoice.contract_id:
        if invoice.contract_id in held:
            return invoice.contract_id, None
        return None, f"{invoice.contract_id} isn't among the contracts held"
    lanes = {f.lane for f in lines if f.lane}
    cur = invoice.payload.get("currency")
    cands = [c for c in sorted(held)
             if lanes and lanes <= {r.lane for r in terms.view.get("rate", contract_id=c)}
             and terms.contract_currency(c) in (None, cur)]
    if len(cands) == 1:
        return cands[0], f"no agreement printed; {cands[0]} is the only contract with these lanes"
    return None, "no agreement printed, and the lanes don't single out one contract"


def _carrier_says(terms: Terms, fact: Fact, contract: str | None) -> bool:
    """A status email counts as a record only in the sender's own words, from the carrier (or, if we can't tell the
    carrier's domain, from either company)."""
    if fact.payload.get("from_quoted_text"):
        return False
    sender = domain(fact.person_email or fact.payload.get("sender_email"))
    party = terms.auth.carrier_party(contract) if contract else None
    allowed = terms.auth.party_domains.get(party or "", set()) or terms.auth.known_domains
    return not allowed or sender in allowed


def related_awbs(view, awb: str | None) -> list[str]:
    """The waybill, then its master (a house's events and declarations may sit on the master)."""
    if not awb:
        return []
    out = [awb]
    for s in view.get("shipment", awb=awb):
        if s.payload.get("master_awb"):
            out.append(norm.awb(s.payload["master_awb"]))
    for s in view.kind("shipment"):
        if any(norm.awb(h.get("awb") or "") == awb for h in s.payload.get("houses") or []):
            out.append(s.awb)
    return list(dict.fromkeys(out))


def find_pickup(terms: Terms, awb: str | None, lines: list[Fact], contract: str | None) -> Pickup:
    view = terms.view
    awbs = related_awbs(view, awb)
    notes: list[str] = []

    def first(found: list[tuple[date, Fact]], source: str, what: str) -> Pickup:
        found.sort(key=lambda x: (x[0], x[1].id))
        if len({d for d, _ in found}) > 1:
            notes.append(f"{what} gives {len({d for d, _ in found})} pickup dates; using the earliest")
        return Pickup(found[0][0], source, found[0][1], notes)

    for a in awbs:
        ev = [(day(f.payload.get("date")), f) for f in view.get("tracking_event", awb=a)
              if f.payload.get("event") == "PICKED_UP" and day(f.payload.get("date"))]
        if ev:
            return first(ev, "tracking", f"the tracking export for {a}")
    for a in awbs:
        ev = [(day(f.payload.get("date")), f) for f in view.get("status_update", awb=a)
              if f.payload.get("event") == "PICKED_UP" and day(f.payload.get("date"))
              and _carrier_says(terms, f, contract)]
        if ev:
            return first(ev, "status_email", f"carrier emails for {a}")
    for a in awbs:
        ev = [(day(f.payload.get("executed_on")), f) for f in view.get("shipment", awb=a)
              if day(f.payload.get("executed_on"))]
        if ev:
            return first(ev, "awb", f"the waybill {a}")
    ev = [(day(f.payload.get("pickup_date")), f) for f in lines if day(f.payload.get("pickup_date"))]
    if ev:
        return first(ev, "invoice", "the invoice")
    return Pickup(None, None, None, notes + ["no pickup date anywhere"])


def _events(terms: Terms, awbs: list[str], contract: str | None) -> dict[str, list[tuple[date, Fact]]]:
    out: dict[str, list[tuple[date, Fact]]] = defaultdict(list)
    for a in awbs:
        for f in terms.view.get("tracking_event", awb=a):
            if day(f.payload.get("date")):
                out[str(f.payload.get("event"))].append((day(f.payload.get("date")), f))
        for f in terms.view.get("status_update", awb=a):
            if day(f.payload.get("date")) and _carrier_says(terms, f, contract):
                out[str(f.payload.get("event"))].append((day(f.payload.get("date")), f))
    for v in out.values():
        v.sort(key=lambda x: (x[0], x[1].id))
    return out


def _weight(view, awb: str | None, lps: list[LinePrice]) -> tuple[Decimal | None, list[str]]:
    """The chargeable weight as printed on the bill (freight line first); the waybill only fills a gap."""
    notes: list[str] = []
    printed = None
    for code in (FREIGHT, "SEC", "PEAK", None):
        for lp in lps:
            if (code is None or lp.code == code) and lp.line.payload.get("unit") == "kg":
                printed = dec(lp.line.payload.get("quantity"))
                if printed is not None:
                    break
        if printed is not None:
            break
    waybill = None
    for a in related_awbs(view, awb)[:1]:
        for s in view.get("shipment", awb=a):
            waybill = dec(s.payload.get("chargeable_kg")) or waybill
    if printed is not None and waybill is not None and printed != waybill:
        notes.append(f"the bill prints {printed} kg, the waybill says {waybill} kg; priced at the printed weight")
    if printed is None and waybill is not None:
        notes.append(f"weight {waybill} kg from the waybill")
    return (printed if printed is not None else waybill), notes


# ---- pricing --------------------------------------------------------------------------------------------------
def price_all(terms: Terms) -> list[InvoicePricing]:
    return [price_invoice(terms, inv) for inv in terms.view.kind("invoice")
            if inv.payload.get("doc_kind") != "credit_note"]


def price_invoice(terms: Terms, invoice: Fact) -> InvoicePricing:
    view = terms.view
    lines = [f for f in view.from_same_doc(invoice, "invoice_line") if f.invoice_id == invoice.invoice_id]
    lines.sort(key=lambda f: (f.payload.get("line_no") or 0, f.id))
    contract, note = contract_for(terms, invoice, lines)
    currency = (invoice.payload.get("currency") or "").upper() or None
    out = InvoicePricing(invoice, contract, currency, [], [], None, None, [note] if note else [])

    groups: dict[str, list[Fact]] = defaultdict(list)
    loose: list[Fact] = []
    for f in lines:
        (groups[f.awb] if f.awb else loose).append(f)
    for awb, group in groups.items():
        out.shipments.append(price_shipment(terms, contract, awb, group, currency))
    doc_date = day(invoice.payload.get("document_date"))
    for f in loose:
        out.other.append(_price_loose(terms, contract, f, currency, doc_date))
    if contract:
        out.tax = _tax(terms, contract, invoice, out.lines(), doc_date)
        if currency and currency != "USD" and doc_date:
            out.fx = terms.fx(contract, norm.month(doc_date))
    return out


def _new_line(terms: Terms, contract: str | None, f: Fact) -> LinePrice:
    lp = LinePrice(f, terms.charge_code(contract, f.payload.get("label")), dec(f.payload.get("amount")) or ZERO)
    lp.sources.append(Source(f, 3, "the billed line"))
    return lp


def price_shipment(terms: Terms, contract: str | None, awb: str | None, lines: list[Fact],
                   currency: str | None) -> ShipmentPricing:
    view = terms.view
    lps = [_new_line(terms, contract, f) for f in lines]
    pickup = find_pickup(terms, awb, lines, contract)
    lane = next((f.lane for f in lines if f.lane), None)
    weight, wnotes = _weight(view, awb, lps)
    sp = ShipmentPricing(awb, lane, pickup, weight, lps, notes=[*pickup.notes, *wnotes])
    on = pickup.date
    for lp in lps:
        if lp.code is None:
            lp.set("not_priced", f"'{lp.line.payload.get('label')}' isn't a charge label in Annex E")
        elif lp.code in NOT_PRICED:
            lp.set("not_priced", f"{lp.code} isn't priced from the rate tables")
        elif contract is None:
            lp.set("unpriceable", "no contract to price against")
        elif on is None:
            lp.set("unpriceable", "no pickup date for this shipment")
    if contract is None or on is None:
        return sp

    pickup_src = Source(pickup.fact, 3, f"pickup date from the {pickup.source}") if pickup.fact else None
    ctx = _Ctx(terms, contract, on, weight, lane, currency, related_awbs(view, awb), sp)
    for lp in [x for x in lps if x.code == FREIGHT]:
        ctx.freight(lp)
    seen: set[str] = set()
    for lp in lps:
        if lp.status != "pending" or lp.code == FREIGHT:
            continue
        if lp.code == FUEL:
            ctx.fuel(lp)
        else:
            ctx.fee(lp, seen)
    for lp in lps:
        if lp.status == "pending":
            lp.set("unpriceable", "nothing priced this line")
        if lp.status in ("priced", "conflict", "missing") and lp.code in (FREIGHT, FUEL):
            lp.cite(pickup_src)
            lp.cite(terms.clause_source(contract, "pricing_date", on))
        elif lp.status in ("priced", "conflict"):
            lp.cite(pickup_src)
    return sp


@dataclass
class _Ctx:
    terms: Terms
    contract: str
    on: date
    weight: Decimal | None
    lane: str | None
    currency: str | None
    awbs: list[str]
    sp: ShipmentPricing

    def _currency_ok(self, lp: LinePrice, cur: str | None) -> bool:
        if cur and self.currency and cur.upper() != self.currency:
            lp.set("unpriceable", f"the contract prices this in {cur}, the bill is in {self.currency}")
            return False
        return True

    # ---- freight ----
    def freight(self, lp: LinePrice) -> None:
        p = lp.line.payload
        lane = lp.line.lane or self.lane
        w = dec(p.get("quantity")) if p.get("unit") == "kg" else self.weight
        if lane is None or w is None:
            lp.set("unpriceable", "no lane" if lane is None else "no chargeable weight")
            return
        r = self.terms.rate(self.contract, lane, self.on, weight=w)
        m = self.terms.rate(self.contract, lane, self.on, kind="minimum")
        minimum = m.value if isinstance(m, Decided) and m.value is not None else None

        def amount(rate: Decimal) -> tuple[Decimal, str]:
            x = to_cents(rate * w)
            if minimum is not None and x < minimum:
                return to_cents(minimum), f"{rate} x {w} kg = {x} is under the {lane} minimum {minimum}"
            return x, f"{rate} x {w} kg = {x}"

        if isinstance(r, Decided) and r.value is None or isinstance(r, Conflict) and any(
                o.value is None for o in r.options):
            lp.set("unpriceable", "a rate row we couldn't read as a number")
            return
        if isinstance(r, Decided):
            if not self._currency_ok(lp, r.sources[0].fact.payload.get("currency")):
                return
            x, step = amount(r.value)
            lp.set("priced", f"{r.label} rate {r.value} for {lane} on {self.on}", x)
            lp.steps.append(step)
            for s in r.sources + (m.sources if minimum is not None and x == minimum else []):
                lp.cite(s)
            lp.steps += r.notes
        elif isinstance(r, Conflict):
            lp.status, lp.why = "conflict", r.why
            for i, opt in enumerate(r.options):
                label = opt.label if opt.label not in lp.options else f"{opt.label} #{i + 1}"
                x, step = amount(opt.value)
                lp.options[label] = x
                lp.steps.append(f"{label}: {step}")
                for s in opt.sources:
                    lp.cite(s)
            for s in r.sources:
                lp.cite(s)
            self.sp.conflict = r
            self.sp.scenarios = list(dict.fromkeys([*self.sp.scenarios, *lp.options]))
        elif isinstance(r, Missing):
            lp.status, lp.missing, lp.why = "missing", r, r.why
            for s in r.sources:
                lp.cite(s)
        else:
            lp.set("unpriceable", r.why)
            lp.steps += r.notes

    # ---- fuel ----
    def fuel(self, lp: LinePrice) -> None:
        pct = self.terms.fuel(self.contract, norm.month(self.on))
        if not isinstance(pct, Decided) or pct.value is None:
            lp.set("unpriceable", pct.why if isinstance(pct, Undefined) else "no fuel %")
            return
        freight = [x for x in self.sp.lines if x.code == FREIGHT]
        for s in pct.sources:
            lp.cite(s)
        lp.cite(self.terms.clause_source(self.contract, "fuel_surcharge", self.on))
        if not freight:
            base = dec(lp.line.payload.get("quantity")) if lp.line.payload.get("rate_is_pct") else None
            if base is None:
                lp.set("unpriceable", "no freight line and no printed base for the fuel %")
                return
            lp.set("priced", f"{pct.value}% of the printed base (the freight isn't on this bill)", pct_of(base, pct.value))
            lp.steps.append(f"{pct.value}% x {base} = {lp.expected}")
            return
        statuses = {x.status for x in freight}
        if "missing" in statuses:
            miss = next(x.missing for x in freight if x.status == "missing")
            lp.status, lp.missing, lp.why = "missing", miss, "rides on freight that depends on " + miss.name
        elif statuses - {"priced", "conflict"}:
            lp.set("unpriceable", "rides on freight that can't be priced")
        elif "conflict" in statuses:
            lp.status, lp.why = "conflict", "rides on freight in conflict"
            for s in self.sp.scenarios:
                base = sum((x.expected_in(s) or ZERO for x in freight), ZERO)
                lp.options[s] = pct_of(base, pct.value)
                lp.steps.append(f"{s}: {pct.value}% x {base} = {lp.options[s]}")
        else:
            base = sum((x.expected for x in freight), ZERO)
            lp.set("priced", f"{pct.value}% fuel for {norm.month(self.on)} on the correct freight",
                   pct_of(base, pct.value))
            lp.steps.append(f"{pct.value}% x {base} = {lp.expected}")

    # ---- Annex D fees ----
    def fee(self, lp: LinePrice, seen: set[str]) -> None:
        r = self.terms.accessorial(self.contract, lp.code, self.on)
        if not isinstance(r, Decided):
            lp.set("unpriceable", r.why if isinstance(r, Undefined) else "no fee")
            return
        a = r.value
        fee = dec(a.get("fee"))
        if fee is None or not self._currency_ok(lp, a.get("currency")):
            if fee is None:
                lp.set("unpriceable", f"Annex D {lp.code} has no readable fee")
            return
        for s in r.sources:
            lp.cite(s)
        code, basis = lp.code, a.get("basis")
        once = basis in ("per_shipment", None) or code in ("DG", "DRYICE", "DLV", "CUSTOMS")
        if once and code in seen:
            lp.set("priced", f"{code} is charged once per shipment and is already on this bill", ZERO)
            return
        handler = {"DG": self._dg, "DRYICE": self._dg, "STOR": self._storage, "DLV": self._delivery,
                   "CUSTOMS": self._customs}.get(code)
        if handler is not None:
            handler(lp, fee)
        else:
            self._by_basis(lp, fee, basis, a.get("months"))
        if lp.status == "priced" and once and (lp.expected or ZERO) > 0:
            seen.add(code)

    def _by_basis(self, lp: LinePrice, fee: Decimal, basis: str | None, months: list | None) -> None:
        p = lp.line.payload
        if months and self.on.month not in [int(m) for m in months]:
            lp.set("priced", f"{lp.code} applies only to pickups in months {months}; picked up {self.on}", ZERO)
            return
        if basis == "per_kg":
            w = dec(p.get("quantity")) if p.get("unit") == "kg" else self.weight
            if w is None:
                lp.set("unpriceable", "no chargeable weight")
                return
            lp.set("priced", f"Annex D {lp.code} {fee} per kg", fee * w)
            lp.steps.append(f"{fee} x {w} kg = {lp.expected}")
        elif basis in ("per_shipment", "per_invoice"):
            lp.set("priced", f"Annex D {lp.code} {fee} {basis.replace('_', ' ')}", fee)
        elif basis == "per_month":
            qty = dec(p.get("quantity")) if p.get("unit") == "month" else Decimal(1)
            lp.set("priced", f"Annex D {lp.code} {fee} per month", fee * (qty or 1))
        elif basis == "per_day":
            qty = dec(p.get("quantity")) if p.get("unit") == "day" else None
            if qty is None:
                lp.set("unpriceable", "a per-day fee with no day count")
                return
            lp.set("priced", f"Annex D {lp.code} {fee} per day for {qty} days as printed", fee * qty)
        else:
            lp.set("unpriceable", f"Annex D {lp.code} basis {basis!r} isn't something we price")

    def _declaration(self) -> Fact | None:
        for a in self.awbs:
            found = self.terms.view.get("dg_declaration", awb=a)
            if found:
                return max(found, key=lambda f: (f.known_at, f.id))
        return None

    def _dg(self, lp: LinePrice, fee: Decimal) -> None:
        decl = self._declaration()
        if decl is None:
            lp.set("unpriceable", f"no shipper's declaration held for {self.sp.awb}")
            return
        p = decl.payload
        other = bool(p.get("has_dg_other_than_dry_ice"))
        dry = bool(p.get("contains_dry_ice") or p.get("dry_ice_only") or
                   any(isinstance(i, dict) and i.get("is_dry_ice") for i in p.get("items") or []))
        lp.cite(Source(decl, 3, "the shipper's declaration"))
        if lp.code == "DG":
            lp.cite(self.terms.clause_source(self.contract, "dry_ice" if not other else "dangerous_goods", self.on))
            if other:
                lp.set("priced", "the declaration lists dangerous goods other than dry ice", fee)
            elif dry:
                lp.set("priced", "dry ice used as a refrigerant doesn't attract the DG fee", ZERO)
            else:
                lp.set("unpriceable", "the declaration shows neither dangerous goods nor dry ice")
        else:
            if other:
                lp.cite(self.terms.clause_source(self.contract, "mixed_dg", self.on))
                lp.set("priced", "mixed shipment: only the DG fee applies", ZERO)
            elif dry:
                lp.cite(self.terms.clause_source(self.contract, "dry_ice", self.on))
                lp.set("priced", "dry ice handling, once per shipment", fee)
            else:
                lp.set("unpriceable", "the declaration shows no dry ice")

    def _storage(self, lp: LinePrice, fee: Decimal) -> None:
        ev = _events(self.terms, self.awbs, self.contract)
        rule = self.terms.rule(self.contract, "storage", self.on)
        spec = rule.value if isinstance(rule, Decided) else {}
        arrivals = ev.get(spec.get("arrival_event") or "ARRIVED_FACILITY") or []
        if not arrivals:
            lp.set("unpriceable", "no arrival at the facility on record")
            return
        arrival = arrivals[0]
        releases = [x for name in (spec.get("release_events") or RELEASE_EVENTS) for x in ev.get(name, [])
                    if x[0] >= arrival[0]]
        if not releases:
            lp.set("unpriceable", "no delivery or collection on record")
            return
        release = min(releases, key=lambda x: (x[0], x[1].id))
        free, clause = self.terms.storage_free_days(self.contract, self.on)
        if free is None:
            lp.set("unpriceable", "the contract's free storage days aren't known")
            return
        days = max(0, (release[0] - arrival[0]).days - free)
        lp.set("priced", f"storage {days} days at {fee}", fee * days)
        lp.steps.append(f"{release[0]} - {arrival[0]} - {free} free days = {days} days x {fee} = {lp.expected}")
        for s in (Source(arrival[1], 3, "arrival"), Source(release[1], 3, "release"), clause):
            lp.cite(s)

    def _delivery(self, lp: LinePrice, fee: Decimal) -> None:
        ev = _events(self.terms, self.awbs, self.contract)
        delivered, collected = ev.get("DELIVERED") or [], ev.get("COLLECTED_BY_CONSIGNEE") or []
        lp.cite(self.terms.clause_source(self.contract, "delivery_fee", self.on))
        if delivered:
            lp.cite(Source(delivered[0][1], 3, "delivered"))
            lp.set("priced", "the carrier delivered", fee)
        elif collected:
            lp.cite(Source(collected[0][1], 3, "collected by the consignee"))
            lp.set("priced", "collected by the consignee: the carrier didn't deliver", ZERO)
        else:
            lp.set("unpriceable", "no delivery or collection on record")

    def _customs(self, lp: LinePrice, fee: Decimal) -> None:
        """Customs fees are the shipper's unless the hold came from the carrier's own documents. Who caused it is
        Claude's reading of the reason (cause), from the tracking export's reason code or a customs notice."""
        ev = _events(self.terms, self.awbs, self.contract)
        holds = [f for _, f in ev.get("CUSTOMS_HOLD", [])]
        holds += [f for a in self.awbs for f in self.terms.view.get("customs_notice", awb=a)]
        if not holds:
            lp.set("unpriceable", "no customs hold on record")
            return
        known = [f for f in holds if f.payload.get("cause") not in (None, "not_stated")]
        fact = known[0] if known else holds[0]
        cause = fact.payload.get("cause") or "not_stated"
        lp.cite(Source(fact, 3, "the customs hold"))
        if cause != "carrier_documents":
            lp.cite(self.terms.clause_source(self.contract, "customs_fees", self.on))
            lp.set("priced", f"customs hold ({cause.replace('_', ' ')}) is for the shipper's account", fee)
            return
        rule = self.terms.rule(self.contract, "customs_holds", self.on)
        exempt = rule.value.get("carrier_error_exempt") if isinstance(rule, Decided) else None
        if exempt is None:
            lp.set("unpriceable", "the hold came from the carrier's documents, and the contract's rule on that isn't known")
            return
        lp.cite(rule.sources[0])
        if exempt:
            lp.set("priced", "the hold came from the carrier's own documents, so the fee isn't payable", ZERO)
        else:
            lp.set("priced", "the contract makes customs fees payable even for the carrier's errors", fee)


def _price_loose(terms: Terms, contract: str | None, f: Fact, currency: str | None, doc_date: date | None) -> LinePrice:
    """A line with no waybill: a monthly or per-invoice fee, or something we leave alone."""
    lp = _new_line(terms, contract, f)
    if lp.code is None:
        return lp.set("not_priced", f"'{f.payload.get('label')}' isn't a charge label in Annex E")
    if lp.code in NOT_PRICED:
        return lp.set("not_priced", f"{lp.code} isn't priced from the rate tables")
    if contract is None:
        return lp.set("unpriceable", "no contract to price against")
    period = str(f.payload.get("period") or "")
    on = day(f"{period}-01") if re.fullmatch(r"\d{4}-\d{2}", period) else doc_date
    if on is None:
        return lp.set("unpriceable", "no date to price this fee on")
    r = terms.accessorial(contract, lp.code, on)
    if not isinstance(r, Decided) or r.value.get("basis") not in ("per_month", "per_invoice"):
        return lp.set("unpriceable", "a charge with no waybill that isn't a monthly or per-invoice fee")
    ctx = _Ctx(terms, contract, on, None, None, currency, [], ShipmentPricing(None, None, Pickup(on, None, None), None, [lp]))
    ctx.fee(lp, set())
    return lp


def _countries(lane: str | None) -> tuple[str, str] | None:
    """(origin, destination) country of a lane: station codes in these documents start with the ISO country code
    (INN3 = India, USC3 = United States). A lane that doesn't look like that gives None and GST isn't judged."""
    m = re.fullmatch(r"([A-Z]{2})[A-Z0-9]*-([A-Z]{2})[A-Z0-9]*", lane or "")
    return (m[1], m[2]) if m else None


def _tax(terms: Terms, contract: str, invoice: Fact, lps: list[LinePrice], on: date | None) -> TaxCheck | None:
    """GST, judged only on what the contract says (rules.gst, answered by the contract reader): not charged at all,
    charged on everything, or charged only on domestic lines. Anything less certain is left unjudged."""
    taxes = invoice.payload.get("taxes") or []
    billed = sum((dec(t.get("amount")) or ZERO for t in taxes), ZERO)
    billed += sum((lp.billed for lp in lps if lp.code == TAX), ZERO)
    rule = terms.rule(contract, "gst", on)
    if not isinstance(rule, Decided):
        return TaxCheck(billed, None, "not_priced", "the contract doesn't say how GST applies") if billed else None
    applies, rate = rule.value.get("applies"), dec(rule.value.get("rate_pct"))
    if applies == "not_applicable":
        return TaxCheck(billed, ZERO, "priced", "the contract says no GST is charged", rule.sources)
    if rate is None or applies not in ("domestic_only", "all_supplies"):
        return TaxCheck(billed, None, "not_priced", "the contract gives no GST rate") if billed else None
    base = ZERO
    for lp in lps:
        if lp.code in NOT_PRICED or lp.code is None:
            continue
        if applies == "domestic_only":
            countries = _countries(lp.line.lane)
            if countries is None:
                return TaxCheck(billed, None, "not_priced", "a line's lane doesn't show its countries")
            if countries[0] != countries[1]:
                continue
        if lp.status != "priced":
            return TaxCheck(billed, None, "not_priced", "a taxable line isn't settled")
        base += lp.expected
    scope = "domestic lines" if applies == "domestic_only" else "all lines"
    return TaxCheck(billed, pct_of(base, rate), "priced", f"GST {rate}% on {scope} ({base})", rule.sources)
