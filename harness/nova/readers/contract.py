"""Contracts, amendments and annex updates.

Two reads of the same document, each with one focused job:
1. terms: what the instrument is (MSA, amendment, annex update), who signed it, its dates, its clauses as structured
   terms (payment, late fees, rebates, precedence, storage ...), Schedule 1, and references to other documents
   ("Annex A rev 2 (attached)") that may or may not be in the mailbox;
2. tables: rate periods and rate rows, fuel by month, accessorial fees, charge-code aliases, exchange rates.

The two reads run in parallel (they don't depend on each other). Then two checks, each worth one more read of the
tables with the exact problem named:
- completeness: every short line carrying a decimal number (a rate row, a fuel month, an FX row) must be quoted by
  some extracted item;
- consistency: every period a rate names must be one of the rate periods (matched loosely: "P1" and "Period P1" are
  the same period). A rate whose period still can't be placed is stored without dates, and pricing then treats the
  rates of that row as undecided instead of picking one (knowledge/terms.py).
Whatever is still wrong after that is reported as a warning.

Terms are stored as Claude copied them (strings as printed). The few clauses the engine has to act on (which
instrument prevails, free storage days, GST, customs holds) are also answered as fixed choices under `rules`, so no
code ever has to interpret contract wording. A question the document doesn't settle is left unanswered, and the
engine then abstains or falls back to the glossary's default.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from .. import config
from .. import normalize as norm
from ..llm import Gateway, LLMFailed
from . import values as v
from . import verify
from .base import (AUTOMATION_FIELD, COMMON_RULES, QUOTE, RETRY_NOTE, STR, STR_LIST, SUMMARY_FIELD, SUMMARY_RULE,
                   DocContext,
                   FactOut, ReadOutcome, call, ground, injection_facts, plain, tool)

VERSION = "contract-v3"

TERM_NAMES = [
    "pricing_date", "chargeable_weight", "minimum_charge", "weight_band_rule", "rate_notices", "fuel_surcharge",
    "security_surcharge", "dangerous_goods", "dry_ice", "mixed_dg", "storage", "delivery_fee", "customs_fees",
    "payment_days", "late_fee", "disputed_amounts", "due_date_extension", "dispute_window", "volume_rebate",
    "rebate_credit_due", "qualifying_spend", "handling_margin", "margin_step_down", "improvement_credit",
    "waiver_rules", "notices", "precedence", "contract_year", "gst", "fx_updates", "termination", "other",
]

TERMS_SYSTEM = COMMON_RULES + """

This is a contract document between a shipper and a carrier: a master services agreement, an amendment to one, or an update of one of its annexes. Record:
- instrument: what this document is, the agreement number it is or amends, its own reference, its date, its effective date, and who signed it for each party;
- parties: the shipper and the carrier, with the short party code Schedule 1 uses for each (for example "TBX-US");
- term: when the agreement ends, whether it renews itself, and any notice period for not renewing (for an amendment, only a new end date it sets);
- terms: every clause that affects what may be billed, when it is due, what is owed back, or who may decide things. Use one entry per clause with the best-fitting name and put the clause's content in `value` as an object of short fields, copying numbers as printed. Suggested shapes:
  payment_days {days, from}; late_fee {rate_pct, per, grace_days, compounding: true/false, base}; disputed_amounts {no_fee_while_disputed: true/false}; due_date_extension {who}; dispute_window {days, from, contact}; storage {free_days, arrival_event, release_events, arrival_day_counts, release_day_charged}; fuel_surcharge {applies_to, month_of, excludes}; chargeable_weight {volumetric_divisor, round_up_to}; minimum_charge {rule}; weight_band_rule {rule}; dangerous_goods {rule}; dry_ice {rule}; mixed_dg {rule}; delivery_fee {rule}; customs_fees {rule}; volume_rebate {tiers: [{threshold, rate_pct}], currency, period, applies_to}; rebate_credit_due {days_after_period_end}; qualifying_spend {includes, excludes, credit_notes, conversion}; handling_margin {applicable, pct, base}; margin_step_down {applicable, threshold, measure, period, new_pct, applies_from}; improvement_credit {pct, basis, due_days_after_anniversary}; waiver_rules {who, scope, conditional}; precedence {order, between_amendments, unranked, quoted_text}; rate_notices {who, effective}; contract_year {starts}; gst {rate_pct, applies_when}; fx_updates {who}; notices {rule}; termination {notice_days, rule}.
  Write "applicable": false for a clause that says it does not apply.
- rules: answer these fixed questions, each with its clause number and a quote. Answer only from this document's own words, never from general practice; if the document doesn't settle a question, answer not_stated or leave it out. An amendment answers only what it changes.
  - notice_vs_amendment: when a rate notice (or other correspondence) and a signed amendment give different rates for the same lane and period, which prevails? unranked = the document says neither prevails automatically, or that the parties resolve the difference jointly, or that its precedence clause does not rank correspondence against amendments; amendment_prevails; notice_prevails; later_prevails = whichever takes effect later; not_stated = the document doesn't say.
  - storage: free_days as a whole number; arrival_event = the event that starts the storage count; release_events = the events that end it. Use the event names listed below.
  - gst: applies = domestic_only (charged only on services within one country; exports zero-rated), all_supplies, not_applicable (the agreement says no GST or sales tax is charged), or not_stated; rate_pct as printed.
  - customs_holds: carrier_error_exempt = true when customs hold or examination fees are not payable where the hold came from the carrier's own documents; false when the document says they are payable regardless.
  - late_fee: rate_pct per month as printed; grace_days = how many days past the due date before a fee can accrue ("unpaid more than 10 days after the due date" = 10, "more than 0 days" = 0); compounding = true when unpaid fees are added to the balance the next month's fee is charged on, false when every month's fee is on the principal only.
  - payment_terms: days until an invoice is due, and what they count from (invoice_date, receipt or other).
  - volume_rebate: applicable (false when the clause says it does not apply); period (quarter, contract_year, month or other); every tier with its threshold and rate as printed; threshold_currency; credit_due_days = days after the period ends by which the credit is issued.
  - improvement_credit: applicable; rate_pct as printed; due_days_after_anniversary.
  - qualifying_spend: included = the kinds of charges that count (base_freight, fuel, security, annex_d_accessorials, handling_margin, carrier_cost, duties, taxes, late_fees), only those the definition includes; credit_notes = net_all_dated_in_period (net of every credit note dated in the period), net_only_for_period_invoices, not_netted or not_stated.
  - handling_margin: applicable; rate_pct; step_down_applicable; step_down_threshold as printed; step_down_measure (quarter_qualifying_spend, quarter_billed_total or other); step_down_rate_pct = the rate after the step-down.
  - term: auto_renews; non_renewal_notice_days = days before the end date by which notice of non-renewal must be given; extension_by = signed_instrument_only (only an instrument signed by both parties extends the term), notice_from_authorised_person, or not_stated.
- doc_references: every other document this one relies on by name. name_as_cited is its name exactly as cited but without words saying it is attached or enclosed ("Annex A rev 2 (attached)" -> "Annex A rev 2"; said_to_be_attached records that). Add what it governs, the lanes it covers, from when, who issues it and its category. An amendment that deletes or replaces rates by pointing to another annex or schedule ("The Annex A rates for lane(s) X are deleted and replaced as follows: Annex A rev 2 (attached) applies from ...") cites that document: record it, with its lanes and date, even when it says it is attached. Give an empty list only when the document names no other document;
- schedule1: every Authorised Person with name, title, party code, categories and email address.
Don't record the rate, fuel, fee, charge-code or exchange-rate tables here; a separate read covers them.
""" + SUMMARY_RULE + "\n\n" + v.EVENT_HELP

TABLES_SYSTEM = COMMON_RULES + """

This is a contract document between a shipper and a carrier. Record its tables, every row, nothing skipped:
- rate_periods: each rate period with its label and pickup dates, and whether the last period's rates keep applying after it ends. The label is the short name the rate table's columns use for the period: for a column headed "P1 rate" and a line "Period P1: pickups ...", the label is "P1". Every period named in a rate's values must be one of these labels, spelled the same way;
- rates: every rate row: lane, weight band exactly as printed ("0-44.5 kg", "300+ kg", "minimum charge"), band_kind (per_kg, or minimum_charge for a per-shipment minimum), band_from and band_to copied from the band (leave band_to empty for an open-ended band such as "300+ kg", band_from empty for "up to 44.5 kg"), and its rate for each period column (period = the column's label; leave period empty when one rate applies to every period, as in an amendment); add valid_from / valid_to only when the row itself states dates;
- fuel: the fuel surcharge for each month;
- accessorials: each fee with its code, name, fee, currency and basis as printed (keep conditions such as "for pickups in September to November only" in basis), plus basis_unit (per_kg for per chargeable kg, per_shipment, per_day, per_month, per_invoice, percent, or other) and, only when the fee applies to pickups in certain months, pickup_months as month numbers (September to November = [9, 10, 11]);
- charge_codes: each charge code with its name and every label it may be billed as;
- fx: each exchange-rate row (month and rate as printed, and what the rate is, for example "INR per 1 USD").
An amendment or an annex update usually has only some of these tables: give empty lists for the ones it doesn't have."""

PARTY = {"type": "object", "properties": {"role": {"enum": ["shipper", "carrier", "other"]}, "name": STR, "code": STR,
                                          "quote": QUOTE}, "required": ["role", "name"]}

RANKINGS = ["unranked", "amendment_prevails", "notice_prevails", "later_prevails", "not_stated"]
GST_APPLIES = ["domestic_only", "all_supplies", "not_applicable", "not_stated"]
QS_PARTS = ["base_freight", "fuel", "security", "annex_d_accessorials", "handling_margin", "carrier_cost", "duties",
            "taxes", "late_fees"]
CREDIT_NETTING = ["net_all_dated_in_period", "net_only_for_period_invoices", "not_netted", "not_stated"]
DAYS = {"type": "integer", "minimum": 0}
BOOL = {"type": "boolean"}


def _days(x) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 3660 else None


def _bool(x) -> bool | None:
    return x if isinstance(x, bool) else None


# The page's own signature blocks and effective date, for when the reading leaves them out.
SIGNED = re.compile(r"Signed for and on behalf of (.+?) \(the (Shipper|Carrier)\)\s*\n(?:Signature:[^\n]*\n)?\s*"
                    r"Name:\s*([^\n]+)\n\s*Title:\s*([^\n]+)\n\s*Date:\s*([^\n]+)", re.I)
EFFECTIVE = re.compile(r"Effective from (\d{4}-\d{2}-\d{2})")
RULES = {"type": "object", "properties": {
    "notice_vs_amendment": {"type": "object", "properties": {
        "ranking": {"enum": RANKINGS}, "clause": STR, "quote": QUOTE}, "required": ["ranking"]},
    "storage": {"type": "object", "properties": {
        "free_days": {"type": "integer", "minimum": 0}, "arrival_event": {"enum": v.EVENTS},
        "release_events": {"type": "array", "items": {"enum": v.EVENTS}}, "clause": STR, "quote": QUOTE}},
    "gst": {"type": "object", "properties": {
        "applies": {"enum": GST_APPLIES}, "rate_pct": STR, "clause": STR, "quote": QUOTE}, "required": ["applies"]},
    "customs_holds": {"type": "object", "properties": {
        "carrier_error_exempt": {"type": "boolean"}, "clause": STR, "quote": QUOTE}},
    "late_fee": {"type": "object", "properties": {
        "rate_pct": STR, "grace_days": DAYS, "compounding": BOOL, "clause": STR, "quote": QUOTE}},
    "payment_terms": {"type": "object", "properties": {
        "days": DAYS, "from": {"enum": ["invoice_date", "receipt", "other"]}, "clause": STR, "quote": QUOTE}},
    "volume_rebate": {"type": "object", "properties": {
        "applicable": BOOL, "period": {"enum": ["quarter", "contract_year", "month", "other"]},
        "tiers": {"type": "array", "items": {"type": "object", "properties": {"threshold": STR, "rate_pct": STR},
                                             "required": ["threshold", "rate_pct"]}},
        "threshold_currency": STR, "credit_due_days": DAYS, "clause": STR, "quote": QUOTE}},
    "improvement_credit": {"type": "object", "properties": {
        "applicable": BOOL, "rate_pct": STR, "due_days_after_anniversary": DAYS, "clause": STR, "quote": QUOTE}},
    "qualifying_spend": {"type": "object", "properties": {
        "included": {"type": "array", "items": {"enum": QS_PARTS}}, "credit_notes": {"enum": CREDIT_NETTING},
        "clause": STR, "quote": QUOTE}},
    "handling_margin": {"type": "object", "properties": {
        "applicable": BOOL, "rate_pct": STR, "step_down_applicable": BOOL, "step_down_threshold": STR,
        "step_down_measure": {"enum": ["quarter_qualifying_spend", "quarter_billed_total", "other"]},
        "step_down_rate_pct": STR, "clause": STR, "quote": QUOTE}},
    "term": {"type": "object", "properties": {
        "auto_renews": BOOL, "non_renewal_notice_days": DAYS,
        "extension_by": {"enum": ["signed_instrument_only", "notice_from_authorised_person", "not_stated"]},
        "clause": STR, "quote": QUOTE}},
}}

TERMS_TOOL = tool("record_contract_terms", "Record what a contract document is and what its clauses say.", {
    "instrument": {"type": "object", "properties": {
        "type": {"enum": ["msa", "amendment", "annex_update", "other"]},
        "contract_id": {"type": "string", "description": "the agreement number this document is or amends"},
        "reference": {"type": "string", "description": "this document's own reference (for the agreement itself, its number)"},
        "title": STR, "dated": STR, "effective_from": STR,
        "signatories": {"type": "array", "items": {"type": "object", "properties": {
            "party": STR, "name": STR, "title": STR, "date": STR}, "required": ["name"]}},
        "quote": QUOTE}, "required": ["type", "contract_id"]},
    "parties": {"type": "array", "items": PARTY},
    "currency": STR,
    "term": {"type": "object", "properties": {
        "effective_from": STR, "end_date": STR, "term_text": STR, "auto_renews": {"type": "boolean"},
        "renewal_term": STR, "non_renewal_notice": STR, "extension_rule": STR, "quote": QUOTE}},
    "terms": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"enum": TERM_NAMES}, "clause": STR, "value": {"type": "object"}, "quote": QUOTE},
        "required": ["name", "value", "quote"]}},
    "doc_references": {"type": "array", "items": {"type": "object", "properties": {
        "name_as_cited": STR, "governs": STR, "lanes": STR_LIST, "effective_from": STR, "issued_by": STR,
        "category": {"enum": ["rates", "treasury", "payment_terms", "waivers", "notices", "other"]},
        "said_to_be_attached": {"type": "boolean"}, "quote": QUOTE}, "required": ["name_as_cited", "quote"]}},
    "rules": RULES,
    "schedule1": {"type": "array", "items": {"type": "object", "properties": {
        "name": STR, "title": STR, "party": STR, "categories": STR_LIST, "email": STR, "quote": QUOTE},
        "required": ["name", "categories", "email"]}},
    "automation_instructions": AUTOMATION_FIELD,
    "summary": SUMMARY_FIELD,
}, ["instrument", "terms", "doc_references", "schedule1", "automation_instructions"])

TABLES_TOOL = tool("record_contract_tables", "Record every row of a contract's rate and fee tables.", {
    "rate_periods": {"type": "array", "items": {"type": "object", "properties": {
        "label": STR, "from": STR, "to": STR, "quote": QUOTE}, "required": ["label"]}},
    "last_period_continues": {"type": "boolean"},
    "rates": {"type": "array", "items": {"type": "object", "properties": {
        "lane": STR, "band": STR, "band_kind": {"enum": ["per_kg", "minimum_charge"]}, "band_from": STR,
        "band_to": STR, "currency": STR, "valid_from": STR, "valid_to": STR,
        "values": {"type": "array", "items": {"type": "object", "properties": {"period": STR, "rate": STR},
                                              "required": ["rate"]}},
        "quote": QUOTE}, "required": ["lane", "band", "values", "quote"]}},
    "fuel": {"type": "array", "items": {"type": "object", "properties": {"month": STR, "pct": STR, "quote": QUOTE},
                                       "required": ["month", "pct", "quote"]}},
    "accessorials": {"type": "array", "items": {"type": "object", "properties": {
        "code": STR, "name": STR, "fee": STR, "currency": STR, "basis": STR, "basis_unit": {"enum": v.BASIS_UNITS},
        "pickup_months": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 12}}, "quote": QUOTE},
        "required": ["name", "fee", "quote"]}},
    "charge_codes": {"type": "array", "items": {"type": "object", "properties": {
        "code": STR, "name": STR, "aliases": STR_LIST, "quote": QUOTE}, "required": ["code", "name", "quote"]}},
    "fx": {"type": "array", "items": {"type": "object", "properties": {
        "month": STR, "rate": STR, "unit": STR, "quote": QUOTE}, "required": ["month", "rate", "quote"]}},
    "automation_instructions": AUTOMATION_FIELD,
}, ["rates", "fuel", "accessorials", "charge_codes", "fx", "automation_instructions"])

class ContractReader:
    name = "contract"
    version = VERSION

    def read(self, ctx: DocContext, gateway: Gateway) -> ReadOutcome:
        out = ReadOutcome(self.name, self.version)
        doc = ctx.document_block()
        # terms and tables don't depend on each other: read them side by side (the longest read in a batch)
        terms_out, tables_out = ReadOutcome(self.name, self.version), ReadOutcome(self.name, self.version)
        note = RETRY_NOTE.get()

        def read_one(o, purpose, system, tool_def):
            RETRY_NOTE.set(note)              # a worker thread starts with an empty context
            return call(gateway, o, purpose=purpose, models=config.CONTRACT_MODELS, system=system, tool_def=tool_def,
                        content=doc, max_tokens=16000)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_terms = pool.submit(read_one, terms_out, "contract_terms", TERMS_SYSTEM, TERMS_TOOL)
            f_tables = pool.submit(read_one, tables_out, "contract_tables", TABLES_SYSTEM, TABLES_TOOL)
            terms, tables = f_terms.result(), f_tables.result()
        for o in (terms_out, tables_out):
            out.model = o.model or out.model
            out.input_tokens += o.input_tokens
            out.output_tokens += o.output_tokens
            out.warnings += o.warnings
            out.repaired = out.repaired or o.repaired
        out.summary = terms_out.summary
        unplaced = unknown_periods(tables)
        if unplaced:
            known = ", ".join(repr(str(p.get("label"))) for p in tables.get("rate_periods") or []) or "none"
            again = call(gateway, out, purpose="contract_tables", models=config.CONTRACT_MODELS, system=TABLES_SYSTEM,
                         tool_def=TABLES_TOOL, max_tokens=16000,
                         content=doc + "\n\nA first reading named periods in the rates that aren't among its rate "
                                       f"periods: rates use {', '.join(map(repr, unplaced))}, rate_periods are {known}. "
                                       "Record the tables again, complete, with each rate period's label written "
                                       "exactly as the rates name it.")
            if len(unknown_periods(again)) < len(unplaced):
                tables = again
        terms = self._cited_as_attached(ctx, gateway, out, doc, terms)
        missed = self._uncovered(ctx, terms, tables)
        if missed:
            listed = "\n".join(f"- {line}" for line in missed[:60])
            again = call(gateway, out, purpose="contract_tables", models=config.CONTRACT_MODELS, system=TABLES_SYSTEM,
                         tool_def=TABLES_TOOL, max_tokens=16000,
                         content=doc + "\n\nA first reading missed these lines, which look like table rows. Record the "
                                       "tables again, complete, including them:\n" + listed)
            if len(self._uncovered(ctx, terms, again)) < len(missed):
                tables = again
            missed = self._uncovered(ctx, terms, tables)
        facts, problems = build(ctx, terms, tables)
        problems += [f"table-like line not extracted: {line}" for line in missed]
        out.doc_type = (terms.get("instrument") or {}).get("type")
        out.facts = facts
        out.warnings = out.warnings + problems
        out.status = "partial" if out.warnings else "ok"
        return out

    @staticmethod
    def _cited_as_attached(ctx: DocContext, gateway: Gateway, out: ReadOutcome, doc: str, terms: dict) -> dict:
        """A backstop for the one reference that decides a cannot_determine: an amendment (or any instrument but the
        MSA, whose annexes are its own) that names a document as attached or enclosed, with no doc_reference for it.
        One more reading of the terms, naming those lines; only its doc_references are taken."""
        if ((terms.get("instrument") or {}).get("type") or "") == "msa":
            return terms
        text = "\n".join(t for _, t in ctx.page_texts)
        cited = {m.group(1).strip() for m in ATTACHED.finditer(text)}
        have = {_flat(r.get("name_as_cited")) for r in terms.get("doc_references") or []}
        missing = sorted(n for n in cited if _flat(n) and not any(_flat(n) in h or h in _flat(n) for h in have if h))
        if not missing:
            return terms
        lines = [line.strip() for line in text.splitlines() if any(n in line for n in missing)]
        try:
            again = call(gateway, out, purpose="contract_terms", models=config.CONTRACT_MODELS, system=TERMS_SYSTEM,
                         tool_def=TERMS_TOOL, max_tokens=16000,
                         content=doc + "\n\nNote from the audit system, not part of the document: this document "
                                       "cites " + ", ".join(repr(n) for n in missing) + " as attached or enclosed, "
                                       "and a first reading recorded no doc_references for it. Record the terms "
                                       "again, with every document it cites under doc_references. The lines:\n"
                                       + "\n".join(f"- {line}" for line in lines[:10]))
        except LLMFailed as e:          # the first reading stands; the gap is written down
            out.warnings.append(f"cited as attached but not recorded as a document reference: {', '.join(missing)} "
                                f"(a second reading failed: {e})")
            return terms
        found = [r for r in again.get("doc_references") or [] if _flat(r.get("name_as_cited"))
                 and _flat(r.get("name_as_cited")) not in have]
        if found:
            terms = {**terms, "doc_references": list(terms.get("doc_references") or []) + found}
        else:
            out.warnings.append("cited as attached but not recorded as a document reference: " + ", ".join(missing))
        return terms

    @staticmethod
    def _uncovered(ctx: DocContext, terms: dict, tables: dict) -> list[str]:
        quotes = [x.get("quote") for section in ("terms", "doc_references", "schedule1") for x in terms.get(section) or []]
        quotes += [x.get("quote") for section in ("rate_periods", "rates", "fuel", "accessorials", "charge_codes", "fx")
                   for x in tables.get(section) or []]
        quotes += [(terms.get(k) or {}).get("quote") for k in ("instrument", "term")]
        return verify.uncovered_lines(ctx.page_texts, [q for q in quotes if q])


# A rule read from an amendment (any instrument but the MSA) counts only when its quote speaks of the rule's subject.
RULE_SUBJECT = {
    "volume_rebate": r"\brebate", "improvement_credit": r"\bimprovement", "handling_margin": r"\bmargin",
    "storage": r"\bstorage|\bfree days?\b|\bwarehous", "customs_holds": r"\bcustoms\b",
    "payment_terms": r"\bpay(ment|able)?\b|\bdue\b", "late_fee": r"\blate\b",
    "term": r"\bterm\b|\bexpir|\bextend|\bextension\b|\brenew|\bend date",
    "qualifying_spend": r"\bqualifying\b", "notice_vs_amendment": r"\bnotices?\b|\bprevail|\bprecedence",
    "gst": r"\bgst\b|\btax",
}
# The words that name each kind of charge in a Qualifying Spend definition.
QS_WORDS = {"base_freight": r"\bfreight\b", "fuel": r"\bfuel\b", "security": r"\bsecurity\b",
            "annex_d_accessorials": r"\bannex d\b|\baccessorial", "handling_margin": r"\bmargin\b",
            "carrier_cost": r"\bcarrier'?s? costs?\b", "duties": r"\bdut(y|ies)\b", "taxes": r"\btax(es)?\b|\bgst\b",
            "late_fees": r"\blate fees?\b"}
EXCLUDES = re.compile(r"\b(?:exclud\w*|does not include|do not include|not including)\b", re.I)

ATTACHED = re.compile(r"([A-Z](?:(?!\.\s+[A-Z])[A-Za-z0-9 .\-/]){1,40}?)\s*\((?:attached|enclosed)\)")


def _flat(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def period_key(label) -> str:
    """A period's name reduced to what identifies it: "Period P1", "P1 rate", "Period P1: pickups 2035-01-06 to ..."
    and "p1" all become "p1"."""
    s = re.split(r"[:(]", str(label or "").lower(), maxsplit=1)[0]
    s = re.sub(r"\b(rates?|period|periods|column|pickups?)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def match_periods(periods: dict[str, tuple], label: str) -> tuple | None:
    """The (from, to) of the rate period a rate's period name refers to, or None if it can't be placed for sure."""
    if not label:
        return None
    if label in periods:
        return periods[label]
    want = period_key(label)
    keyed: dict[str, list] = {}
    for name, span in periods.items():
        keyed.setdefault(period_key(name), []).append(span)
    hits = keyed.get(want) or []
    return hits[0] if want and len(hits) == 1 else None


def unknown_periods(tables: dict) -> list[str]:
    """The period names the rates use that no rate period matches."""
    periods = {str(p.get("label") or "").strip(): (None, None) for p in tables.get("rate_periods") or []}
    names = {str(val.get("period") or "").strip() for r in tables.get("rates") or [] for val in r.get("values") or []}
    return sorted(n for n in names if n and match_periods(periods, n) is None)


def build(ctx: DocContext, terms: dict, tables: dict) -> tuple[list[FactOut], list[str]]:
    problems: list[str] = []
    facts = injection_facts(ctx, (terms.get("automation_instructions") or []) +
                            (tables.get("automation_instructions") or []))
    inst = terms.get("instrument") or {}
    if not v.text_or_none(inst.get("contract_id")):
        return facts, ["no agreement number found: nothing from this document can be stored against a contract"]
    contract = norm.code(inst["contract_id"])
    itype = inst.get("type") or "other"
    effective = v.iso(v.parse_date(inst.get("effective_from")) or v.parse_date((terms.get("term") or {}).get("effective_from")))
    reference = v.text_or_none(inst.get("reference")) or contract
    signatories = plain(inst.get("signatories") or [])
    page_text = "\n".join(t for _, t in ctx.page_texts)
    if not any(s.get("name") for s in signatories):     # the reading missed the signature blocks printed on the page
        signatories = [{"party": m.group(2).lower(), "name": m.group(3).strip(), "title": m.group(4).strip(),
                        "date": m.group(5).strip()} for m in SIGNED.finditer(page_text)]
    if not effective:                                    # ... or the "Effective from" line
        m = EFFECTIVE.search(page_text)
        effective = v.iso(v.parse_date(m.group(1))) if m else None
    signed_parties = {norm.code(s.get("party") or s.get("name") or "") for s in signatories if s.get("name")}

    def fact(kind, payload, quote, check, keys=None, **extra):
        page, q, ver = ground(ctx, quote, check)
        if ver == "unverified":
            problems.append(f"{kind} isn't on the page as quoted: {str(quote)[:80]!r}")
        facts.append(FactOut(kind, payload, keys={"contract_id": contract, **(keys or {})}, page=page, quote=q,
                             verification=ver, **extra))

    fact("instrument", {"type": itype, "reference": reference, "title": v.text_or_none(inst.get("title")),
                        "dated": v.iso(v.parse_date(inst.get("dated"))), "effective_from": effective,
                        "signatories": signatories, "signed_by_both": len(signed_parties) >= 2},
         inst.get("quote"), [inst.get("contract_id")], valid_from=effective)

    term = terms.get("term") or {}
    if itype == "msa":
        end = v.iso(v.parse_date(term.get("end_date")))
        fact("contract", {"title": v.text_or_none(inst.get("title")), "effective_from": effective, "end_date": end,
                          "term_text": v.text_or_none(term.get("term_text")), "auto_renews": term.get("auto_renews"),
                          "renewal_term": v.text_or_none(term.get("renewal_term")),
                          "non_renewal_notice": v.text_or_none(term.get("non_renewal_notice")),
                          "extension_rule": v.text_or_none(term.get("extension_rule")),
                          "parties": plain(terms.get("parties") or []),
                          "currency": (v.text_or_none(terms.get("currency")) or "").upper() or None},
             term.get("quote") or inst.get("quote"), [term.get("end_date")], valid_from=effective, valid_to=end)
    elif v.parse_date(term.get("end_date")):   # an amendment that moves the end date
        fact("contract_notice", {"notice_type": "extension", "new_end_date": v.iso(v.parse_date(term["end_date"])),
                                 "effective_from": effective, "instrument_ref": reference},
             term.get("quote") or inst.get("quote"), [term.get("end_date")], valid_from=effective)

    for t in terms.get("terms") or []:
        fact("contract_term", {"term": t.get("name"), "clause": v.text_or_none(t.get("clause")),
                               "value": plain(t.get("value") or {}), "instrument": itype, "instrument_ref": reference},
             t.get("quote"), [], valid_from=effective)

    rules = terms.get("rules") or {}

    def rule(name, value, item, check):
        quote = str(item.get("quote") or "")
        if itype != "msa" and not re.search(RULE_SUBJECT.get(name, name.replace("_", " ")), quote, re.I):
            # an amendment changes only what it prints: "All other terms of the Agreement are unchanged" is not a
            # rebate clause, and must not switch the MSA's rebate off (practice runs 2 and 4)
            problems.append(f"rule {name} from the {itype} left out: its quote doesn't speak of it ({quote[:60]!r})")
            return
        fact("contract_term", {"term": f"rule:{name}", "clause": v.text_or_none(item.get("clause")), "value": value,
                               "instrument": itype, "instrument_ref": reference},
             item.get("quote"), check, valid_from=effective)

    nva = rules.get("notice_vs_amendment") or {}
    ranking = v.choice(nva.get("ranking"), RANKINGS)
    if ranking and ranking != "not_stated":
        rule("notice_vs_amendment", {"ranking": ranking}, nva, [])
    st = rules.get("storage") or {}
    free = st.get("free_days")
    if isinstance(free, int) and not isinstance(free, bool) and 0 <= free <= 365:
        rule("storage", {"free_days": free, "arrival_event": v.choice(st.get("arrival_event"), v.EVENTS),
                         "release_events": [e for e in st.get("release_events") or [] if e in v.EVENTS]},
             st, [str(free)])
    elif free is not None:
        problems.append(f"free storage days {free!r} isn't a whole number of days")
    gst = rules.get("gst") or {}
    applies = v.choice(gst.get("applies"), GST_APPLIES)
    if applies and applies != "not_stated":
        rate = v.parse_pct(gst.get("rate_pct"))
        rule("gst", {"applies": applies, "rate_pct": rate}, gst, [gst.get("rate_pct")] if rate is not None else [])
    ch = rules.get("customs_holds") or {}
    if isinstance(ch.get("carrier_error_exempt"), bool):
        rule("customs_holds", {"carrier_error_exempt": ch["carrier_error_exempt"]}, ch, [])
    lf = rules.get("late_fee") or {}
    if v.parse_pct(lf.get("rate_pct")) is None:          # the reading put the clause in the general terms list instead
        t = next((t for t in terms.get("terms") or [] if t.get("name") == "late_fee" and isinstance(t.get("value"), dict)),
                 None)
        lf = {**t["value"], "quote": t.get("quote"), "clause": t.get("clause")} if t else {}
    if v.parse_pct(lf.get("rate_pct")) is not None:
        rule("late_fee", {"rate_pct": v.parse_pct(lf["rate_pct"]), "grace_days": _days(lf.get("grace_days")),
                          "compounding": _bool(lf.get("compounding"))}, lf, [lf["rate_pct"]])
    pt = rules.get("payment_terms") or {}
    if _days(pt.get("days")) is not None:
        rule("payment_terms", {"days": pt["days"], "from": v.choice(pt.get("from"), ["invoice_date", "receipt", "other"],
                                                                  "other")}, pt, [str(pt["days"])])
    vr = rules.get("volume_rebate") or {}
    tiers = [{"threshold": v.parse_amount(t.get("threshold")), "rate_pct": v.parse_pct(t.get("rate_pct"))}
             for t in vr.get("tiers") or [] if isinstance(t, dict)]
    tiers = sorted((t for t in tiers if t["threshold"] is not None and t["rate_pct"] is not None),
                   key=lambda t: t["threshold"])
    check_vals = [t.get("threshold") for t in vr.get("tiers") or [] if isinstance(t, dict) and t.get("threshold")]
    if not tiers and vr.get("quote"):
        q_matches = re.findall(r"(?:[A-Z]{3}\s*)?([0-9,]+(?:\.[0-9]+)?)\s*:\s*([0-9.]+)%", str(vr["quote"]))
        if q_matches:
            tiers = sorted(([{"threshold": v.parse_amount(m[0]), "rate_pct": v.parse_pct(m[1])} for m in q_matches]),
                           key=lambda t: t["threshold"])
            tiers = [t for t in tiers if t["threshold"] is not None and t["rate_pct"] is not None]
            check_vals = [m[0] for m in q_matches]
    if tiers:
        rule("volume_rebate", {"applicable": True, "period": v.choice(vr.get("period"), ["quarter", "contract_year",
                                                                                         "month", "other"], "quarter"),
                               "tiers": tiers, "threshold_currency": (v.text_or_none(vr.get("threshold_currency"))
                                                                      or "USD").upper(),
                               "credit_due_days": _days(vr.get("credit_due_days")) or 45},
             vr, check_vals)
    elif vr.get("applicable") is False:
        rule("volume_rebate", {"applicable": False}, vr, [])
    ic = rules.get("improvement_credit") or {}
    if ic.get("applicable") is False:
        rule("improvement_credit", {"applicable": False}, ic, [])
    elif v.parse_pct(ic.get("rate_pct")) is not None:
        rule("improvement_credit", {"applicable": True, "rate_pct": v.parse_pct(ic["rate_pct"]),
                                    "due_days_after_anniversary": _days(ic.get("due_days_after_anniversary"))},
             ic, [ic["rate_pct"]])
    qs = rules.get("qualifying_spend") or {}
    parts = sorted({p for p in qs.get("included") or [] if p in QS_PARTS})
    named = EXCLUDES.split(str(qs.get("quote") or ""), maxsplit=1)[0]
    if named.strip():     # keep only the kinds the definition itself names, before any "excludes"
        unnamed = [p for p in parts if not re.search(QS_WORDS[p], named, re.I)]
        if unnamed:
            problems.append(f"qualifying spend: {', '.join(unnamed)} left out, the definition doesn't name them")
            parts = [p for p in parts if p not in unnamed]
    if parts:
        rule("qualifying_spend", {"included": parts,
                                  "credit_notes": v.choice(qs.get("credit_notes"), CREDIT_NETTING, "not_stated")}, qs, [])
    hm = rules.get("handling_margin") or {}
    if hm.get("applicable") is False:
        rule("handling_margin", {"applicable": False}, hm, [])
    elif hm.get("applicable") is True:
        rule("handling_margin", {
            "applicable": True, "rate_pct": v.parse_pct(hm.get("rate_pct")),
            "step_down_applicable": _bool(hm.get("step_down_applicable")),
            "step_down_threshold": v.parse_amount(hm.get("step_down_threshold")),
            "step_down_measure": v.choice(hm.get("step_down_measure"), ["quarter_qualifying_spend",
                                                                        "quarter_billed_total", "other"], "other"),
            "step_down_rate_pct": v.parse_pct(hm.get("step_down_rate_pct"))},
            hm, [x for x in (hm.get("rate_pct"), hm.get("step_down_rate_pct")) if x])
    tm = rules.get("term") or {}
    if any(tm.get(k) is not None for k in ("auto_renews", "non_renewal_notice_days", "extension_by")):
        rule("term", {"auto_renews": _bool(tm.get("auto_renews")),
                      "non_renewal_notice_days": _days(tm.get("non_renewal_notice_days")),
                      "extension_by": v.choice(tm.get("extension_by"), ["signed_instrument_only",
                                                                        "notice_from_authorised_person", "not_stated"],
                                               "not_stated")},
             tm, [str(tm["non_renewal_notice_days"])] if _days(tm.get("non_renewal_notice_days")) else [])

    for r in terms.get("doc_references") or []:
        fact("doc_reference", {"name_as_cited": r.get("name_as_cited"), "governs": v.text_or_none(r.get("governs")),
                               "lanes": [norm.lane(x) for x in r.get("lanes") or [] if x],
                               "effective_from": v.iso(v.parse_date(r.get("effective_from"))),
                               "issued_by": v.text_or_none(r.get("issued_by")), "category": r.get("category") or "other",
                               "said_to_be_attached": bool(r.get("said_to_be_attached")),
                               "cited_in": reference},
             r.get("quote"), [r.get("name_as_cited")])

    for p in terms.get("schedule1") or []:
        email = v.text_or_none(p.get("email"))
        if not email:
            problems.append(f"Schedule 1 entry without an email address: {p.get('name')}")
            continue
        fact("schedule1_person", {"name": p.get("name"), "title": v.text_or_none(p.get("title")),
                                  "categories": [c.strip().lower() for c in p.get("categories") or [] if c],
                                  "instrument_ref": reference},
             p.get("quote"), [email], keys={"party": p.get("party") or "UNKNOWN", "person_email": email},
             valid_from=effective)

    # ---- tables ----
    periods = {}
    for p in tables.get("rate_periods") or []:
        periods[str(p.get("label") or "").strip()] = (v.iso(v.parse_date(p.get("from"))), v.iso(v.parse_date(p.get("to"))))
    last_label = list(periods)[-1] if periods else None
    continues = bool(tables.get("last_period_continues"))
    currency = (v.text_or_none(terms.get("currency")) or "").upper() or None

    for r in tables.get("rates") or []:
        kind, lo, hi, issue = v.band_of(r)
        if issue:
            problems.append(issue)
        if kind == "unknown":
            problems.append(f"couldn't read the weight band {r.get('band')!r}")
        for val in r.get("values") or []:
            rate = v.parse_amount(val.get("rate"))
            label = str(val.get("period") or "").strip()
            span = match_periods(periods, label)
            if span is not None:
                start, end = span
                if continues and span == periods.get(last_label):
                    end = None
            elif label:
                problems.append(f"rate for unknown period {label!r} on {r.get('lane')}")
                start, end = effective, None
            else:  # one rate for every period: an amendment or a single-rate table, in force from its effective date
                start, end = effective, None
            start = v.iso(v.parse_date(r.get("valid_from"))) or start
            end = v.iso(v.parse_date(r.get("valid_to"))) or end
            fact("rate", {"kind": kind, "band_min": lo, "band_max": hi, "band_text": r.get("band"), "rate": rate,
                          "currency": (v.text_or_none(r.get("currency")) or currency or "").upper() or None,
                          "period_label": label or None, "instrument": itype, "instrument_ref": reference},
                 r.get("quote"), [r.get("lane"), val.get("rate")], keys={"lane": r.get("lane")},
                 valid_from=start, valid_to=end)

    for f in tables.get("fuel") or []:
        month = v.parse_month(f.get("month"))
        if not month:
            problems.append(f"couldn't read the fuel month {f.get('month')!r}")
            continue
        fact("fuel_rate", {"pct": v.parse_pct(f.get("pct")), "instrument_ref": reference}, f.get("quote"),
             [f.get("pct")], keys={"period": month})

    for a in tables.get("accessorials") or []:
        basis = v.text_or_none(a.get("basis"))
        fact("accessorial", {"code": norm.code(a["code"]) if v.text_or_none(a.get("code")) else None,
                             "name": a.get("name"), "fee": v.parse_amount(a.get("fee")),
                             "currency": (v.text_or_none(a.get("currency")) or currency or "").upper() or None,
                             "basis": v.choice(a.get("basis_unit"), v.BASIS_UNITS[:-1]), "basis_text": basis,
                             "months": v.months_list(a.get("pickup_months")),
                             "instrument_ref": reference},
             a.get("quote"), [a.get("fee")], valid_from=effective)

    for c in tables.get("charge_codes") or []:
        labels = [x for x in [c.get("name"), *(c.get("aliases") or [])] if x]
        fact("charge_alias", {"code": norm.code(c["code"]), "name": c.get("name"), "labels": labels,
                              "label_keys": sorted({norm.label(x) for x in labels}), "instrument_ref": reference},
             c.get("quote"), [c.get("code")], valid_from=effective)

    for x in tables.get("fx") or []:
        month = v.parse_month(x.get("month"))
        if not month:
            problems.append(f"couldn't read the exchange-rate month {x.get('month')!r}")
            continue
        fact("fx_rate", {"rate": v.parse_amount(x.get("rate")), "unit": v.text_or_none(x.get("unit")),
                         "instrument_ref": reference},
             x.get("quote"), [x.get("rate")], keys={"period": month})

    return facts, problems
