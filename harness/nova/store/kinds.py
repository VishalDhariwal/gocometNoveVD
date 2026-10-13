"""Every kind of fact a reader may store, and the lookup keys each one must carry.

The rest of a fact (its payload) is JSON that each reader defines and validates itself. The store only enforces what
everything downstream relies on: a known kind, its keys filled in, keys normalised, no floats for money.

Authority is not stored here. Who sent a waiver or a rate notice is kept (person_email); whether that person was
allowed to is decided at report time against Schedule 1 and feedback, because both can change after the email arrives.
"""
from __future__ import annotations

from dataclasses import dataclass

KEY_COLUMNS = ("contract_id", "invoice_id", "awb", "lane", "party", "person_email", "period")


@dataclass(frozen=True)
class Kind:
    name: str
    keys: tuple[str, ...]   # key columns that must be filled
    about: str


_ALL = (
    # ---- contract papers ----
    Kind("contract", ("contract_id",),
         "header: parties and party codes, currency, dated, effective and end dates, renewal and notice terms"),
    Kind("contract_term", ("contract_id",),
         "one modelled term with its clause: payment days, late fee, grace, compounding, dispute window, storage free "
         "days, rebate tiers, QS definition, improvement credit, margin step-down, precedence rules"),
    Kind("contract_clause", ("contract_id",),
         "a clause's number, heading and text, for citations and for rules we don't model as data"),
    Kind("schedule1_person", ("contract_id", "party", "person_email"),
         "an Authorised Person: name, title, categories (rates, waivers, payment_terms, notices, billing_disputes, "
         "treasury)"),
    Kind("rate", ("contract_id", "lane"),
         "one rate row: weight band or minimum, rate, currency, the instrument it came from (MSA annex, amendment, "
         "rate notice) and who sent it; valid_from / valid_to are its pickup dates"),
    Kind("fuel_rate", ("contract_id", "period"), "fuel surcharge % for a pickup month"),
    Kind("accessorial", ("contract_id",), "an Annex D fee: code, fee, currency, basis, conditions"),
    Kind("charge_alias", ("contract_id",), "an Annex E code and every label it may be billed as"),
    Kind("fx_rate", ("contract_id", "period"), "INR per 1 USD for a calendar month"),
    Kind("instrument", ("contract_id",),
         "an MSA, amendment, rate notice or annex update: type, reference, effective dates, signatories, sender"),
    Kind("doc_reference", ("contract_id",),
         "a document another one points to, such as 'Annex A rev 2 (attached)': the name as cited, who issues it, "
         "what it governs"),
    # ---- bills ----
    Kind("invoice", ("invoice_id",),
         "an invoice or statement header: number as printed, issuer, contract reference, currency, dates, totals, "
         "copy / reminder markers"),
    Kind("invoice_line", ("invoice_id",),
         "one billed line: AWB, label, lane, quantity and unit, rate, amount, printed pickup date, period"),
    Kind("credit_note", (),
         "a credit note: number, date, credit type, period, amount, currency, invoices referenced; contract_id and "
         "invoice_id keys are filled when the note names them"),
    # ---- shipments ----
    Kind("shipment", ("awb",),
         "an air waybill: master or house, pieces, actual and chargeable weight, dimensions, origin, destination, "
         "executed date"),
    Kind("dg_declaration", ("awb",), "a shipper's declaration: UN numbers, classes, dry-ice-only flag"),
    Kind("tracking_event", ("awb",), "one tracking row: event, date, station, reason code"),
    Kind("status_update", ("awb",), "a carrier status email: event and date"),
    Kind("customs_notice", ("awb",), "a customs hold: dates, reason code, whose error caused it"),
    # ---- payments ----
    Kind("ledger_entry", ("invoice_id",),
         "one AP register row: register date, invoice date, due date, amount, currency, status, paid date"),
    # ---- correspondence ----
    Kind("waiver", ("person_email",),
         "a waiver: charges, AWBs, invoices, months, any condition, and whether it was only seen quoted"),
    Kind("due_date_extension", ("invoice_id", "person_email"), "an invoice's due date moved to a new date"),
    Kind("payment_request", ("invoice_id",),
         "an email asking for payment of an invoice, and any copy / reminder / for-your-records marker"),
    Kind("contract_notice", ("contract_id",), "an extension, renewal or termination notice"),
    Kind("injection_attempt", (),
         "text that tries to instruct automated readers: kept for the audit, never acted on"),
)

KINDS: dict[str, Kind] = {k.name: k for k in _ALL}
