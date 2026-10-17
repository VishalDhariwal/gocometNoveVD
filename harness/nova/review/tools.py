"""The reviewer's tools: one Session per case, holding the calculation ledger.

Four kinds, all read-only over what was received by as_of (MCP-style schemas, served in-process):

  context     read_document, search_documents, documents_for, find_facts, contract_terms, payment_register,
              tracking
  engine      price_invoice     code's own pricing of every billed line, with its working
              reprice_shipment  the same pricing with the reviewer's corrected inputs (pickup date, chargeable
                                weight, freight rate per kg), each backed by a quote that must be on its page.
                                Bands, minimums, fuel on the correct freight, fees and rounding stay code's.
              late_fee          the contract's late-fee arithmetic for given dates and amounts
  arithmetic  calc              Decimal sums, differences, products and percentages, half up to cents
  assembly    finding_amount    billed minus correct per line, added up in the invoice's currency, converted to USD
                                once at the Annex F rate: the only way a finding gets an amount

Every number a calculation tool returns is in the ledger under an id (calc#n) with its inputs and working. The final
decision names a finding_amount id instead of writing a number, so the amount in the report is always code's.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .. import normalize as norm
from ..decide.common import ZERO, amount_of, fee_schedule, invoice_date, lines_of, months_late, to_usd
from ..knowledge.pricing import price_shipment
from ..knowledge.view import day
from .library import ANSWER_CHARS, INJECTED, Library, clip, compact

CONTRACT_TOPICS = {"rates": ("rate",), "fuel": ("fuel_rate",), "fees": ("accessorial",), "charge_codes": ("charge_alias",),
                   "rules": ("contract_term", "contract"), "fx": ("fx_rate",), "people": ("schedule1_person",),
                   "documents": ("instrument", "doc_reference")}


def _dec(x) -> Decimal | None:
    try:
        return Decimal(str(x).replace(",", "").strip()) if x not in (None, "") else None
    except InvalidOperation:
        return None


def cents(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Calc:
    id: str
    kind: str                 # line | calc | late_fee | finding_amount
    value: Decimal            # in `currency`
    currency: str | None
    working: list[str]
    invoice_id: str | None = None
    lines: list[int] = field(default_factory=list)
    native: Decimal | None = None          # finding_amount: before conversion
    sources: list = field(default_factory=list)   # facts behind it (Source), for the evidence
    checked: list = field(default_factory=list)   # quotes the reviewer gave for corrected inputs (library.Checked)
    pickup_from: list = field(default_factory=list)   # repriced with a pickup date: what kind of record gave it


class Session:
    """One case's tools and its ledger. Runs in a worker thread; reads only the Library's in-memory copies."""

    def __init__(self, lib: Library, contract: str | None):
        self.lib, self.contract = lib, contract
        self.calcs: dict[str, Calc] = {}

    def _add(self, kind: str, value: Decimal, currency: str | None, working: list[str], **kw) -> Calc:
        c = Calc(f"calc#{len(self.calcs) + 1}", kind, value, currency, working, **kw)
        self.calcs[c.id] = c
        return c

    def call(self, name: str, args: dict) -> str:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"There is no tool called {name}."
        try:
            return clip(fn(**{k: v for k, v in (args or {}).items() if v not in (None, "")}), ANSWER_CHARS)
        except TypeError as e:
            return f"Wrong arguments for {name}: {e}"

    # ---- context ----------------------------------------------------------------------------------------------
    def t_read_document(self, doc: str, page: int | None = None) -> str:
        return self.lib.read_document(doc, int(page) if page else None)

    def t_search_documents(self, text: str) -> str:
        return self.lib.search_documents(str(text))

    def t_documents_for(self, reference: str) -> str:
        ref = compact(reference)
        if len(ref) < 4:
            return "Give an invoice number, a waybill or a contract reference."
        inv = norm.invoice_id(reference)
        docs = self.lib.related(inv) if inv in self.lib.headers else []
        docs += [d for i, d in sorted(self.lib.docs.items()) if ref in self.lib.flat.get(i, "") and d not in docs]
        if not docs:
            return f"No document received by the as-of date names {reference!r}."
        return json.dumps({"documents": [self.lib.describe(d) for d in docs[:30]]}, ensure_ascii=False)

    def t_find_facts(self, kind: str | None = None, invoice_id: str | None = None, awb: str | None = None,
                     contract_id: str | None = None) -> str:
        return self.lib.find_facts(kind, invoice_id, awb, contract_id)

    def t_contract_terms(self, topic: str, contract_id: str | None = None, lane: str | None = None) -> str:
        contract = contract_id or self.contract
        kinds = CONTRACT_TOPICS.get(topic)
        if kinds is None:
            return f"Topics: {', '.join(CONTRACT_TOPICS)}."
        if not contract:
            return "Which contract? Give contract_id."
        facts = [f for k in kinds for f in self.lib.view.get(k, contract_id=contract)]
        if lane:
            facts = [f for f in facts if not f.lane or f.lane == norm.lane(lane)]
        if not facts:
            return f"Nothing on {topic} for {contract} as of this date."
        rows = [self.lib.fact_row(f) for f in sorted(facts, key=lambda f: (f.valid_from or "", f.id))[:60]]
        return json.dumps({"contract": contract, "topic": topic, "facts": rows}, ensure_ascii=False, default=str)

    def t_payment_register(self, invoice_id: str) -> str:
        return self.lib.payment_register(invoice_id)

    def t_tracking(self, awb: str) -> str:
        facts = [f for k in ("tracking_event", "status_update", "shipment", "customs_notice", "dg_declaration")
                 for f in self.lib.view.get(k, awb=awb)]
        if not facts:
            return f"No tracking, status email, waybill or declaration for {awb} as of this date."
        rows = [self.lib.fact_row(f) for f in sorted(facts, key=lambda f: (f.payload.get("date") or "", f.id))]
        return json.dumps({"awb": norm.awb(awb), "records": rows[:40]}, ensure_ascii=False, default=str)

    # ---- engine -----------------------------------------------------------------------------------------------
    def t_price_invoice(self, invoice_id: str) -> str:
        inv = norm.invoice_id(invoice_id)
        ip = self.lib.pricing(inv)
        if ip is None:
            return f"No invoice {invoice_id} received by the as-of date."
        rows = []
        for lp in ip.lines():
            row = {"fact_id": lp.line.id, "label": lp.line.payload.get("label"), "awb": lp.line.awb,
                   "charge_code": lp.code, "billed": str(lp.billed), "status": lp.status, "why": lp.why}
            if lp.status == "priced" and lp.expected is not None:
                c = self._add("line", lp.expected, ip.currency, [lp.why, *lp.steps], invoice_id=inv,
                              lines=[lp.line.id], sources=list(lp.sources))
                row.update(correct=str(lp.expected), calc=c.id)
            if lp.options:
                row["options"] = {k: str(v) for k, v in lp.options.items()}
            rows.append(row)
        out = {"invoice": inv, "contract": ip.contract, "currency": ip.currency, "lines": rows}
        if ip.tax is not None:
            out["tax"] = {"billed": str(ip.tax.billed), "correct": None if ip.tax.expected is None else
                          str(ip.tax.expected), "status": ip.tax.status, "why": ip.tax.why}
        return json.dumps(out, ensure_ascii=False)

    def t_reprice_shipment(self, invoice_id: str, awb: str, pickup_date: str | None = None,
                           weight_kg: str | None = None, rate_per_kg: str | None = None,
                           evidence: list | None = None) -> str:
        """Price one shipment's lines again with corrected inputs. Each input needs a quote (evidence) that is on
        its page; code checks it now and refuses the input if it isn't."""
        lib, inv = self.lib, norm.invoice_id(invoice_id)
        h = lib.headers.get(inv)
        if h is None:
            return f"No invoice {invoice_id} received by the as-of date."
        lines = [f for f in lines_of(lib.view, h) if f.awb and norm.awb(f.awb) == norm.awb(awb)]
        if not lines:
            return f"Invoice {inv} has no lines for waybill {awb}."
        ov = {}
        if pickup_date:
            ov["pickup"] = day(pickup_date)
        if weight_kg:
            ov["weight"] = _dec(weight_kg)
        if rate_per_kg:
            ov["rate"] = _dec(rate_per_kg)
        if not ov or any(v is None for v in ov.values()):
            return "Give at least one of pickup_date (YYYY-MM-DD), weight_kg or rate_per_kg, as plain values."
        checked = [lib.check(e, self.contract, None) for e in evidence or []]
        good = [c for c in checked if c is not None and c.verified and c.why != INJECTED and c.level <= 3]
        if not good:
            return ("Not accepted: each corrected input needs a quote, copied exactly from a document received by "
                    "the as-of date and from a source that can state it (a record, the contract, or an authorised "
                    "person), in evidence: [{doc, page, quote}].")
        with lib._lock:
            sp = price_shipment(lib.terms, lib.contract.get(inv), awb, lines, (h.payload.get("currency") or "").upper()
                                or None, overrides=ov)
        given = ", ".join(f"{k} {v}" for k, v in ov.items())
        rows = []
        pickup_from = None
        if "pickup" in ov:
            d = lib.docs.get(good[0].doc_id)
            pickup_from = ("tracking" if d is not None and d.format == "xlsx" else
                           "status_email" if "status email" in good[0].why else "other")
        for lp in sp.lines:
            row = {"fact_id": lp.line.id, "label": lp.line.payload.get("label"), "charge_code": lp.code,
                   "billed": str(lp.billed), "status": lp.status, "why": lp.why}
            if lp.status == "priced" and lp.expected is not None:
                c = self._add("line", lp.expected, (h.payload.get("currency") or "").upper() or None,
                              [f"repriced with {given} ({good[0].doc} p{good[0].page})", lp.why, *lp.steps],
                              invoice_id=inv, lines=[lp.line.id], sources=list(lp.sources), checked=good[:2],
                              pickup_from=[pickup_from] if pickup_from else [])
                row.update(correct=str(lp.expected), calc=c.id)
            rows.append(row)
        return json.dumps({"invoice": inv, "awb": norm.awb(awb), "inputs": given, "lines": rows}, ensure_ascii=False)

    def t_late_fee(self, principal: str, rate_pct: str, due_date: str, paid_date: str, grace_days: int = 0,
                   compounding: bool = False, currency: str | None = None) -> str:
        p, r, due, paid = _dec(principal), _dec(rate_pct), day(due_date), day(paid_date)
        if None in (p, r, due, paid):
            return "Give principal and rate_pct as numbers, due_date and paid_date as YYYY-MM-DD."
        late = (paid - due).days
        n = months_late(late) if late > int(grace_days or 0) else 0
        fees = fee_schedule(p, r, n, bool(compounding))
        total = sum(fees, ZERO)
        c = self._add("late_fee", total, currency, [
            f"paid {paid}, due {due}: {late} days late, grace {grace_days}, so {n} fee month(s)",
            f"{'compounding' if compounding else 'simple'} at {r}% on {p}: " + (" + ".join(map(str, fees)) or "0")
            + f" = {total}"])
        return json.dumps({"days_late": late, "fee_months": n, "fees": [str(f) for f in fees], "total": str(total),
                           "calc": c.id})

    # ---- arithmetic -------------------------------------------------------------------------------------------
    def t_calc(self, op: str, values: list, currency: str | None = None, note: str | None = None) -> str:
        nums = [x if isinstance(x, Decimal) else self._value(x) for x in values or []]
        if not nums or any(x is None for x in nums):
            return "values: numbers, or calc ids (calc#n) from earlier results."
        if op == "sum":
            v, how = sum(nums, ZERO), " + ".join(map(str, nums))
        elif op == "subtract" and len(nums) == 2:
            v, how = nums[0] - nums[1], f"{nums[0]} - {nums[1]}"
        elif op == "multiply" and len(nums) == 2:
            v, how = nums[0] * nums[1], f"{nums[0]} x {nums[1]}"
        elif op == "percent" and len(nums) == 2:
            v, how = nums[0] * nums[1] / 100, f"{nums[1]}% of {nums[0]}"
        else:
            return "op: sum (any number of values), subtract / multiply / percent (two values)."
        v = cents(v)
        c = self._add("calc", v, currency, [f"{how} = {v}" + (f" ({note})" if note else "")])
        return json.dumps({"result": str(v), "calc": c.id})

    def _value(self, x) -> Decimal | None:
        if isinstance(x, str) and x.strip().startswith("calc#"):
            c = self.calcs.get(x.strip())
            return c.value if c else None
        return _dec(x)

    # ---- assembly ---------------------------------------------------------------------------------------------
    def t_finding_amount(self, invoice_id: str, lines: list) -> str:
        """lines: [{line: fact_id, correct: calc id | "0"}]. Billed minus correct per line (never below 0), added
        up in the invoice's currency, converted to USD once."""
        lib, inv = self.lib, norm.invoice_id(invoice_id)
        h = lib.headers.get(inv)
        if h is None:
            return f"No invoice {invoice_id} received by the as-of date."
        own = {f.id: f for f in lines_of(lib.view, h)}
        currency = (h.payload.get("currency") or "").upper() or None
        total, steps, used, sources, checked, pickup_from = ZERO, [], [], [], [], []
        for item in lines or []:
            f = own.get(item.get("line") if isinstance(item, dict) else None)
            if f is None:
                return f"{item}: not a billed line of {inv}. Use the fact_id of a line on this invoice."
            correct = str(item.get("correct", "")).strip()
            if correct in ("0", "0.00"):
                exp, how = ZERO, "nothing owed"
            else:
                c = self.calcs.get(correct)
                if c is None:
                    return f"{correct!r} isn't a calc id from this case. Use price_invoice, reprice_shipment, " \
                           f"late_fee or calc first, or \"0\" when nothing is owed for the line."
                if c.currency and currency and c.currency != currency:
                    return f"{c.id} is in {c.currency}; invoice {inv} is in {currency}."
                exp, how = c.value, f"{c.id}: {c.working[-1] if c.working else c.value}"
                sources += c.sources
                checked += c.checked
                pickup_from += c.pickup_from
            over = max(ZERO, amount_of(f) - exp)
            total += over
            used.append(f.id)
            steps.append(f"{f.payload.get('label')} on {f.awb or inv}: billed {amount_of(f)}, correct {exp} "
                         f"({how}), over {over}")
        if not used:
            return "Name at least one billed line."
        usd, fx = to_usd(lib.terms, lib.contract.get(inv), currency, total, invoice_date(h))
        if usd is None:
            return f"Can't convert {currency} {total} to USD: {getattr(fx, 'why', 'no rate')}. If the Annex F rate " \
                   f"for that month is missing, the answer is cannot_determine."
        steps.append(f"Total {total}" + (f" {currency} = USD {usd} at the Annex F rate" if usd != total else ""))
        c = self._add("finding_amount", usd, "USD", steps, invoice_id=inv, lines=used, native=total,
                      sources=sources, checked=checked, pickup_from=sorted(set(pickup_from)))
        return json.dumps({"invoice": inv, "amount_usd": str(usd), "native": str(total), "currency": currency,
                           "calc": c.id, "working": steps}, ensure_ascii=False)


def _schema(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


S, I = {"type": "string"}, {"type": "integer"}
EVIDENCE = {"type": "array", "items": {"type": "object", "properties": {"doc": S, "page": I, "quote": S},
                                       "required": ["doc", "page", "quote"]}}
TOOLS = [
    {"name": "read_document", "description": "Read one page of a document received by the as-of date (page 1 if "
                                             "you give none); the answer says how many pages it has. A one-page "
                                             "document comes whole. Emails show which part is quoted.",
     "input_schema": _schema({"doc": S, "page": I}, ["doc"])},
    {"name": "search_documents", "description": "Find documents whose text contains a phrase, invoice number or "
                                                "waybill. Returns snippets.",
     "input_schema": _schema({"text": S}, ["text"])},
    {"name": "documents_for", "description": "Every document that names an invoice, waybill or contract reference.",
     "input_schema": _schema({"reference": S}, ["reference"])},
    {"name": "find_facts", "description": "Facts code read from the documents, by kind and keys. Kinds include "
                                          "invoice, invoice_line, credit_note, waiver, payment_request, "
                                          "due_date_extension, ledger_entry, tracking_event, status_update, "
                                          "shipment, rate, accessorial, charge_alias, schedule1_person.",
     "input_schema": _schema({"kind": S, "invoice_id": S, "awb": S, "contract_id": S}, [])},
    {"name": "contract_terms", "description": "The contract's terms by topic: rates (optionally one lane), fuel, "
                                              "fees (Annex D), charge_codes (Annex E), rules (clauses), fx (Annex "
                                              "F), people (Schedule 1), documents (instruments and references).",
     "input_schema": _schema({"topic": {"type": "string", "enum": list(CONTRACT_TOPICS)}, "contract_id": S,
                              "lane": S}, ["topic"])},
    {"name": "payment_register", "description": "The shipper's payment register rows for one invoice.",
     "input_schema": _schema({"invoice_id": S}, ["invoice_id"])},
    {"name": "tracking", "description": "Tracking events, carrier status emails, the waybill, customs notices and "
                                        "declarations for one waybill.",
     "input_schema": _schema({"awb": S}, ["awb"])},
    {"name": "price_invoice", "description": "Code's pricing of every billed line on an invoice under the contract, "
                                             "with the working. Each priced line comes with a calc id for its correct "
                                             "amount.",
     "input_schema": _schema({"invoice_id": S}, ["invoice_id"])},
    {"name": "reprice_shipment", "description": "Price one shipment's lines again with corrected inputs (pickup_date, "
                                                "weight_kg, rate_per_kg), each backed by a quote in evidence. The "
                                                "contract's bands, minimums, fuel and rounding still apply. Returns "
                                                "calc ids for the correct amounts.",
     "input_schema": _schema({"invoice_id": S, "awb": S, "pickup_date": S, "weight_kg": S, "rate_per_kg": S,
                              "evidence": EVIDENCE}, ["invoice_id", "awb", "evidence"])},
    {"name": "late_fee", "description": "The late fee owed under the contract's rule (look it up with "
                                        "contract_terms rules): months late = ceil(days late / 30) past the grace "
                                        "days, each month rounded half up, simple or compounding.",
     "input_schema": _schema({"principal": S, "rate_pct": S, "due_date": S, "paid_date": S, "grace_days": I,
                              "compounding": {"type": "boolean"}, "currency": S},
                             ["principal", "rate_pct", "due_date", "paid_date"])},
    {"name": "calc", "description": "Exact arithmetic, rounded half up to cents: sum, subtract, multiply, percent. "
                                    "values may be numbers or calc ids.",
     "input_schema": _schema({"op": {"type": "string", "enum": ["sum", "subtract", "multiply", "percent"]},
                              "values": {"type": "array", "items": S}, "currency": S, "note": S}, ["op", "values"])},
    {"name": "finding_amount", "description": "The amount of a finding: for each billed line, billed minus correct "
                                              "(a calc id, or \"0\" when nothing is owed for it), added up in the "
                                              "invoice's currency and converted to USD once. The decision must name "
                                              "this calc id.",
     "input_schema": _schema({"invoice_id": S, "lines": {"type": "array", "items": {
         "type": "object", "properties": {"line": I, "correct": S}, "required": ["line", "correct"]}}},
         ["invoice_id", "lines"])},
]
