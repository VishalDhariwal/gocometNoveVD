"""Tests for phase 4: one verdict per finding key, the claim ledger, and each class's rules.

Worlds are written straight into the store in the readers' shapes (see test_knowledge.World). The numbers are the
worked examples' (data/examples/*/EXPLANATION.md), moved onto the test contract's dates where needed.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from nova.decide.adjudicate import adjudicate  # noqa: E402
from nova.decide.common import fee_schedule, quarter_bounds  # noqa: E402
from nova.knowledge.view import View  # noqa: E402
from tests.test_knowledge import C, FINTAN, JOAQUIN, P1, F, World  # noqa: E402

SIGNE = ("signe.underhill@tarrowby.example", "Signe Underhill")       # waivers, payment_terms
BILLING = ("billing@tarrowby.example", "Tarrowby Billing")
OUTSIDER = ("ops.desk@tarrowby.example", "Ops Desk")                  # carrier domain, not on Schedule 1


def key(label: str) -> str:
    return "".join(ch for ch in label.lower() if ch.isalnum())


def L(label, amount, awb=None, lane=None, pickup=None, qty=None, unit=None, rate=None, **extra):
    return {"label": label, "amount": amount, "awb": awb, "lane": lane, "pickup": pickup, "qty": qty, "unit": unit,
            "rate": rate, **extra}


class Mailbox(World):
    """World plus the documents phase 4 needs."""

    def papers(self, received=P1[0] + "T09:00:00Z", effective=P1[0], end=None, auto=None, rules=None, aliases=None,
               fx=None, ref="AMD-R"):
        doc = self.email(received, "farid.underhill@quillmere.example", "Farid Underhill", f"Signed {ref}",
                         f"{C}-{ref}.pdf")
        inst = {"instrument": "amendment", "instrument_ref": f"{C}/{ref}"}
        facts = [F("instrument", {"type": "amendment", "reference": f"{C}/{ref}", "effective_from": effective,
                                  "signed_by_both": True}, contract_id=C, valid_from=effective)]
        if end:
            facts.append(F("contract", {"effective_from": effective, "end_date": end, "auto_renews": auto,
                                        "currency": "USD"}, contract_id=C, valid_from=effective,
                           quote=f"2.2 Initial term. The initial term ends on {end}."))
        for name, value in (rules or {}).items():
            facts.append(F("contract_term", {"term": f"rule:{name}", "clause": "7", "value": value, **inst},
                           contract_id=C, valid_from=effective, quote=f"The {name} clause as printed in the contract."))
        for code, labels in (aliases or {}).items():
            facts.append(F("charge_alias", {"code": code, "name": labels[0], "labels": labels,
                                            "label_keys": sorted(key(x) for x in labels), "instrument_ref": ref},
                           contract_id=C, valid_from=effective, quote=f"Code {code}: also billed as {labels[0]}"))
        for month, rate in (fx or {}).items():
            facts.append(F("fx_rate", {"rate": rate, "unit": "INR per 1 USD", "instrument_ref": ref}, contract_id=C,
                           period=month, quote=f"{month} {rate} INR per USD"))
        return self.put(doc, *facts)

    def bill(self, number, received, lines, *, doc_date=None, total=None, markers=(), currency="USD", asks=None,
             contract=C):
        aid = self.email(received, *BILLING, f"Invoice {number}", f"{number.replace('/', '-')}.pdf")
        facts = [F("invoice", {"number_as_printed": number, "doc_kind": "invoice", "currency": currency,
                               "document_date": doc_date or received[:10], "taxes": [], "total": total,
                               "marker_kinds": list(markers)},
                   invoice_id=number, contract_id=contract, quote=f"INVOICE {number} as printed on the bill")]
        for i, ln in enumerate(lines, start=1):
            p = {"line_no": i, "label": ln["label"], "label_key": key(ln["label"]), "awb": ln["awb"],
                 "lane": ln["lane"], "pickup_date": ln["pickup"], "quantity": ln["qty"], "unit": ln["unit"],
                 "rate": ln["rate"], "rate_is_pct": False, "amount": ln["amount"], "currency": currency,
                 "period": ln.get("period"), "refers_to_invoice": ln.get("refers"),
                 "document_date": doc_date or received[:10]}
            facts.append(F("invoice_line", p, invoice_id=number, awb=ln["awb"], lane=ln["lane"], contract_id=contract,
                           quote=f"{i} {ln['awb'] or '-'} {ln['label']} {ln['amount']}"))
        self.put(aid, *facts)
        if asks is not None:
            parent = self.store.document(aid)["parent_id"]
            self.put(parent, F("payment_request", {"sender_email": BILLING[0], "from_quoted_text": False,
                                                   "asks_for_payment": asks, "says_outstanding": asks,
                                                   "marker": None, "marker_kind": "none"},
                               invoice_id=number, person_email=BILLING[0], quote=f"Please pay invoice {number} now."))
        return aid

    def register(self, received, rows):
        doc = self.email(received, "ap@quillmere.example", "AP", "AP register", "ap-register.xlsx", fmt="xlsx")
        self.put(doc, *[F("ledger_entry", {"invoice_number_as_printed": inv, "invoice_date": d, "due_date": due,
                                           "amount": amt, "currency": "USD", "status": status, "paid_date": paid,
                                           "register_as_of": received[:10], "register_received_at": received},
                          invoice_id=inv, quote=f"{inv} | {d} | due {due} | USD {amt} | {status} | {paid or '-'}")
                        for inv, d, due, amt, status, paid in rows])

    def said(self, received, who, kind, payload, quoted=False, original=None, **keys):
        sender, name = who
        doc = self.email(received, sender, name, kind)
        base = {"sender_email": sender, "from_quoted_text": quoted, "original_sender": original, "received_at": received}
        self.put(doc, F(kind, {**base, **payload}, quote=f"{kind}: as written in the email body", **keys))

    def waiver(self, received, who, awbs, charges, quoted=False, original=None, condition=None, **cond):
        payload = {"charges": charges, "charge_keys": [key(c) for c in charges], "awbs": awbs, "invoices": [],
                   "months": [], "condition": condition, "condition_kind": cond.get("kind", "none"),
                   "condition_date": cond.get("by"), "condition_invoice": cond.get("invoice"), "scope_words": None}
        email = original[0] if original else who[0]
        self.said(received, who, "waiver", payload, quoted=quoted, original=original[1] if original else None,
                  person_email=email)

    def credit(self, received, number, amount, credit_class, period=None, refers=(), lines=()):
        doc = self.email(received, *BILLING, f"Credit note {number}", f"{number}.pdf")
        self.put(doc, F("credit_note", {"number_as_printed": number, "doc_kind": "credit_note", "currency": "USD",
                                        "document_date": received[:10], "credit_class": credit_class,
                                        "period": period, "refers_to_invoices": list(refers), "amount": amount,
                                        "lines": list(lines)},
                        contract_id=C, invoice_id=refers[0] if len(refers) == 1 else None,
                        quote=f"CREDIT NOTE {number} as printed"))

    def verdicts(self, as_of):
        return {v.finding_key: v for v in adjudicate(View.load(self.store, as_of))}


class Base(unittest.TestCase):
    def setUp(self):
        self.w = Mailbox()
        self.w.msa()

    def tearDown(self):
        self.w.close()

    def get(self, as_of, fkey):
        return self.w.verdicts(as_of).get(fkey)


# ---- overcharge ------------------------------------------------------------------------------------------------
class Overcharge(Base):
    def setUp(self):
        super().setUp()
        self.w.invoice("TX-US-2032-5394", "2032-01-03T09:00:00Z", [
            ("Air freight", "TE56915687", "USE2-INE2", "2031-12-25", "241", "kg", "6.69", "1612.29"),
            ("Fuel surcharge", "TE56915687", "USE2-INE2", "2031-12-25", "1612.29", None, "20.16%", "325.04")])
        self.w.invoice("TX-US-2032-5397", "2032-01-27T09:00:00Z", [
            ("Air freight", "TE68271803", "USE2-INE2", "2032-01-18", "44", "kg", "7.73", "340.12")])
        self.w.tracking("2032-02-29T09:00:00Z", [("TE56915687", "PICKED_UP", "2031-12-25", "-"),
                                                  ("TE68271803", "PICKED_UP", "2032-01-18", "-")])

    def test_finding_and_its_decoy(self):
        vs = self.w.verdicts("2032-08-31")
        v = vs["overcharge|TX-US-2032-5394"]
        self.assertEqual((v.verdict, v.amount_usd, v.remedy), ("finding", D("196.92"), "dispute"))
        quotes = [e.fact.quote for e in v.evidence]
        self.assertEqual(quotes[0], "1 TE56915687 2031-12-25 Air freight USE2-INE2 241 kg 6.69 1612.29")
        self.assertIn("USE2-INE2 100-299.5 kg 6.01 6.16", quotes)
        self.assertIn("TE56915687 | PICKED_UP | 2031-12-25 | USE2 | -", quotes)
        self.assertEqual(v.checks["pickup_sources"], ["tracking"])
        self.assertEqual(vs["overcharge|TX-US-2032-5397"].verdict, "no_finding")

    def test_absent_before_the_invoice_arrives(self):
        self.assertNotIn("overcharge|TX-US-2032-5394", self.w.verdicts("2031-12-31"))


class ConflictAndMissing(Base):
    def setUp(self):
        super().setUp()
        self.w.amendment(f"{C}/AMD-1", "2032-03-16T09:00:00Z", "2032-03-16",
                         rates=[("USW1-USC3", "100-299.5 kg", "2.88")],
                         refs=[("Annex A rev 2 (attached)", ["USS2-INN1"], "2032-06-04")])

    def bill_conflict(self, who):
        self.w.notice("2032-06-02T09:00:00Z", who, [("USW1-USC3", "100-299.5 kg", "3.03")], "2032-06-01")
        self.w.invoice("TX-US-2032-5166", "2032-06-26T09:00:00Z", [
            ("Air freight", "TE92942629", "USW1-USC3", "2032-06-17", "113", "kg", "3.03", "342.39"),
            ("Fuel surcharge", "TE92942629", "USW1-USC3", "2032-06-17", "342.39", None, "23.14%", "79.23")])

    def test_conflict_range(self):
        self.bill_conflict(FINTAN)
        v = self.get("2032-11-30", "overcharge|TX-US-2032-5166")
        self.assertEqual((v.verdict, v.amount_range, v.amount_usd, v.remedy),
                         ("conflict", (D("0.00"), D("20.87")), None, "escalate"))
        self.assertTrue(any("neither prevails" in e.fact.quote for e in v.evidence))

    def test_unauthorised_notice_is_a_plain_finding(self):
        self.bill_conflict(JOAQUIN)
        v = self.get("2032-11-30", "overcharge|TX-US-2032-5166")
        self.assertEqual((v.verdict, v.amount_usd), ("finding", D("20.87")))

    def test_missing_annex(self):
        self.w.invoice("TX-US-2032-0237", "2032-06-20T09:00:00Z", [
            ("Air freight", "TX5", "USS2-INN1", "2032-06-10", "150", "kg", "7.79", "1168.50")])
        v = self.get("2032-09-30", "overcharge|TX-US-2032-0237")
        self.assertEqual((v.verdict, v.remedy), ("cannot_determine", "request_document"))
        self.assertEqual(v.missing_doc, {"name": "Annex A rev 2", "holder": {
            "party": "TBX-US", "person": "Fintan Corrigan", "role": "Pricing Manager"}})
        self.assertTrue(any("Annex A rev 2" in e.fact.quote for e in v.evidence))


# ---- duplicates ------------------------------------------------------------------------------------------------
class Duplicates(Base):
    def resend(self, markers=(), asks=True, paid="2032-02-07"):
        lines = [L("Delivery fee", "1259.23", awb="TE1", lane="USE2-INE2", pickup="2032-01-25", qty="1",
                   unit="shipment")]
        self.w.bill("TX-US-2032-5511", "2032-02-01T09:00:00Z", lines, total="1259.23", asks=True)
        self.w.register("2032-02-10T09:00:00Z", [("TX-US-2032-5511", "2032-01-31", "2032-03-02", "1259.23",
                                                  "paid" if paid else "open", paid)])
        self.w.bill("TX-US-2032-5511", "2032-02-18T09:00:00Z", lines, total="1259.23", markers=markers, asks=asks)

    def test_paid_invoice_sent_again_asking_for_payment(self):
        self.resend()
        v = self.get("2032-03-31", "duplicate|TX-US-2032-5511")
        self.assertEqual((v.verdict, v.amount_usd, v.remedy), ("finding", D("1259.23"), "dispute"))
        self.assertTrue(any("paid" in e.fact.quote for e in v.evidence))

    def test_copy_for_your_records_is_fine(self):
        self.resend(markers=["for_your_records"])
        self.assertEqual(self.get("2032-03-31", "duplicate|TX-US-2032-5511").verdict, "no_finding")

    def test_unpaid_invoice_sent_again_is_fine(self):
        self.resend(paid=None)
        self.assertEqual(self.get("2032-03-31", "duplicate|TX-US-2032-5511").verdict, "no_finding")

    def test_same_charge_rebilled_under_another_label(self):
        self.w.papers(aliases={"SEC": ["Security surcharge", "Screening fee (adj.)"]})
        self.w.bill("TX-US-2032-5436", "2032-01-30T09:00:00Z", [
            L("Security surcharge", "42.08", awb="TE16812897", lane="USE2-INE2", pickup="2032-01-22", qty="263",
              unit="kg", rate="0.16")])
        self.w.bill("TX-US-2032-5441", "2032-05-30T09:00:00Z", [
            L("Screening fee (adj.)", "42.08", awb="TE16812897", lane="USE2-INE2", pickup="2032-05-20", qty="263",
              unit="kg", rate="0.16"),
            L("Screening fee (adj.)", "42.08", awb="TE99999999", lane="USE2-INE2", pickup="2032-05-20", qty="263",
              unit="kg", rate="0.16")])
        vs = self.w.verdicts("2032-06-30")
        v = vs["duplicate|TX-US-2032-5441"]
        self.assertEqual((v.verdict, v.amount_usd), ("finding", D("42.08")))   # the other waybill is a new charge
        self.assertTrue(any("Screening fee (adj.)" in e.fact.quote for e in v.evidence))
        self.assertNotIn("duplicate|TX-US-2032-5436", vs)                       # the earlier bill is the original
        # claim ledger: the repeated line isn't also an overcharge; the original's line still is (0.16 vs 0.15)
        self.assertEqual(vs["overcharge|TX-US-2032-5436"].amount_usd, D("2.63"))
        self.assertEqual(vs["overcharge|TX-US-2032-5441"].amount_usd, D("2.63"))  # only the new waybill's line

    def test_houses_of_one_master_are_not_duplicates(self):
        for n, (house, d) in enumerate((("TE195-H1", "2032-05-14"), ("TE195-H2", "2032-05-20"))):
            self.w.bill(f"TX-US-2032-551{n}", f"{d}T09:00:00Z",
                        [L("Delivery fee", "121.81", awb=house, lane="USE2-INE2", pickup=d, qty="1", unit="shipment")])
        self.assertFalse(any(k.startswith("duplicate") for k in self.w.verdicts("2032-06-30")))

    def test_original_credited_in_full(self):
        self.w.bill("TX-US-2032-6001", "2032-02-01T09:00:00Z",
                    [L("Delivery fee", "121.81", awb="TE7", qty="1", unit="shipment")], total="121.81")
        self.w.credit("2032-02-10T09:00:00Z", "CN-1", "121.81", "correction", refers=["TX-US-2032-6001"])
        self.w.bill("TX-US-2032-6002", "2032-02-15T09:00:00Z",
                    [L("Delivery fee", "121.81", awb="TE7", qty="1", unit="shipment")], total="121.81")
        self.assertEqual(self.get("2032-03-31", "duplicate|TX-US-2032-6002").verdict, "no_finding")


# ---- waivers ---------------------------------------------------------------------------------------------------
class Waivers(Base):
    def bill_delivery(self, awb="TE97049901"):
        self.w.bill("TX-US-2032-5337", "2032-06-28T09:00:00Z", [
            L("Delivery fee", "121.81", awb=awb, lane="USE2-INE2", pickup="2032-06-18", qty="1", unit="shipment")])
        self.w.tracking("2032-06-30T09:00:00Z", [(awb, "PICKED_UP", "2032-06-18", "-"),
                                                  (awb, "DELIVERED", "2032-06-21", "-")])

    def test_authorised_waiver(self):
        self.w.waiver("2032-06-18T09:00:00Z", SIGNE, ["TE97049901"], ["Delivery fee"])
        self.bill_delivery()
        v = self.get("2032-07-31", "waived_but_billed|TX-US-2032-5337")
        self.assertEqual((v.verdict, v.amount_usd, v.remedy), ("finding", D("121.81"), "dispute"))
        self.assertTrue(any(e.fact.kind == "schedule1_person" for e in v.evidence))

    def test_waiver_for_another_waybill(self):
        self.w.waiver("2032-06-18T09:00:00Z", SIGNE, ["TE33628218"], ["Delivery fee"])
        self.bill_delivery()
        self.assertNotIn("waived_but_billed|TX-US-2032-5337", self.w.verdicts("2032-07-31"))

    def test_waiver_from_someone_without_authority(self):
        self.w.waiver("2032-06-18T09:00:00Z", JOAQUIN, ["TE97049901"], ["Delivery fee"])
        self.bill_delivery()
        self.assertEqual(self.get("2032-07-31", "waived_but_billed|TX-US-2032-5337").verdict, "no_finding")

    def test_quoted_waiver_asks_its_sender(self):
        self.w.waiver("2032-06-18T09:00:00Z", OUTSIDER, ["TE97049901"], ["Delivery fee"], quoted=True,
                      original=SIGNE)
        self.bill_delivery()
        v = self.get("2032-07-31", "waived_but_billed|TX-US-2032-5337")
        self.assertEqual((v.verdict, v.missing_doc["holder"]["person"]), ("cannot_determine", "Signe Underhill"))

    def test_quoted_waiver_from_someone_else_asks_the_kam(self):
        self.w.waiver("2032-06-18T09:00:00Z", OUTSIDER, ["TE97049901"], ["Delivery fee"], quoted=True,
                      original=("someone@tarrowby.example", "Some One"))
        self.bill_delivery()
        v = self.get("2032-07-31", "waived_but_billed|TX-US-2032-5337")
        self.assertEqual(v.missing_doc["holder"], {"party": "TBX-US", "person": "Joaquin Rathbone",
                                                   "role": "Key Account Manager"})

    def test_condition_paid_by_a_date(self):
        self.w.waiver("2032-06-18T09:00:00Z", SIGNE, ["TE97049901"], ["Delivery fee"],
                      condition="if invoice TX-US-2032-5300 is paid by 2032-07-10", kind="payment_by_date",
                      by="2032-07-10", invoice="TX-US-2032-5300")
        self.bill_delivery()
        self.w.register("2032-07-05T09:00:00Z", [("TX-US-2032-5300", "2032-06-01", "2032-07-16", "500.00", "open",
                                                  None)])
        self.assertEqual(self.get("2032-07-31", "waived_but_billed|TX-US-2032-5337").verdict, "no_finding")
        self.w.register("2032-07-20T09:00:00Z", [("TX-US-2032-5300", "2032-06-01", "2032-07-16", "500.00", "paid",
                                                  "2032-07-08")])
        self.assertEqual(self.get("2032-07-31", "waived_but_billed|TX-US-2032-5337").verdict, "finding")

    def test_a_line_is_claimed_once(self):
        self.w.waiver("2032-06-18T09:00:00Z", SIGNE, ["TE97049901"], ["Delivery fee"])
        self.w.bill("TX-US-2032-5330", "2032-06-20T09:00:00Z", [
            L("Delivery fee", "121.81", awb="TE97049901", lane="USE2-INE2", pickup="2032-06-18", qty="1",
              unit="shipment")])
        self.bill_delivery()   # the same delivery fee again, on a later invoice
        vs = self.w.verdicts("2032-07-31")
        self.assertEqual(vs["duplicate|TX-US-2032-5337"].verdict, "finding")
        self.assertNotIn("waived_but_billed|TX-US-2032-5337", vs)     # already claimed as a duplicate
        self.assertEqual(vs["waived_but_billed|TX-US-2032-5330"].amount_usd, D("121.81"))


# ---- late fees -------------------------------------------------------------------------------------------------
class LateFees(Base):
    def setUp(self):
        super().setUp()
        self.w.papers(rules={"late_fee": {"rate_pct": "1.86", "grace_days": 5, "compounding": True}},
                      aliases={"LATE": ["Late payment fee", "Late fee"]})

    def test_extended_due_date_means_no_fee(self):
        self.w.register("2032-08-31T09:00:00Z", [
            ("TX-US-2032-5494", "2032-07-11", "2032-08-10", "680.12", "paid", "2032-08-17"),
            ("TX-US-2032-5498", "2032-11-12", "2032-12-02", "4200.07", "paid", "2033-01-01")])
        self.w.said("2032-08-07T09:00:00Z", SIGNE, "due_date_extension",
                    {"new_due_date": "2032-08-25", "new_due_date_printed": "2032-08-25"},
                    invoice_id="TX-US-2032-5494", person_email=SIGNE[0])
        self.w.said("2032-11-20T09:00:00Z", SIGNE, "due_date_extension",
                    {"new_due_date": "2032-12-12", "new_due_date_printed": "2032-12-12"},
                    invoice_id="TX-US-2032-5498", person_email=SIGNE[0])
        self.w.bill("TX-US-2032-5496", "2032-09-06T09:00:00Z", [
            L("Late fee", "12.65", qty="1", unit="month", rate="1.86", refers="TX-US-2032-5494")])
        self.w.bill("TX-US-2033-5582", "2033-01-10T09:00:00Z", [
            L("Late fee", "78.12", qty="1", unit="month", rate="1.86", refers="TX-US-2032-5498")])
        vs = self.w.verdicts("2033-02-28")
        v = vs["late_fee_not_owed|TX-US-2032-5496"]
        self.assertEqual((v.verdict, v.amount_usd, v.remedy), ("finding", D("12.65"), "dispute"))
        self.assertEqual(vs["late_fee_not_owed|TX-US-2033-5582"].verdict, "no_finding")   # 20 days late: owed

    def test_unauthorised_extension_is_ignored(self):
        self.w.register("2032-08-31T09:00:00Z", [
            ("TX-US-2032-5494", "2032-07-11", "2032-08-10", "680.12", "paid", "2032-08-17")])
        self.w.said("2032-08-07T09:00:00Z", JOAQUIN, "due_date_extension", {"new_due_date": "2032-08-25"},
                    invoice_id="TX-US-2032-5494", person_email=JOAQUIN[0])
        self.w.bill("TX-US-2032-5496", "2032-09-06T09:00:00Z", [
            L("Late fee", "12.65", qty="1", unit="month", rate="1.86", refers="TX-US-2032-5494")])
        self.assertEqual(self.get("2032-09-30", "late_fee_not_owed|TX-US-2032-5496").verdict, "no_finding")

    def test_compounding_months(self):
        self.assertEqual(fee_schedule(D("1000"), D("2"), 3, True), [D("20.00"), D("20.40"), D("20.81")])
        self.assertEqual(fee_schedule(D("1000"), D("2"), 3, False), [D("20.00")] * 3)

    def test_risk_window(self):
        self.w.bill("TX-US-2032-0700", "2032-08-12T09:00:00Z", [
            L("Delivery fee", "121.81", awb="TE8", qty="1", unit="shipment")], total="1000.00")
        self.w.register("2032-08-20T09:00:00Z", [("TX-US-2032-0700", "2032-08-12", "2032-09-15", "1000.00", "open",
                                                  None)])
        for as_of, present in (("2032-09-15", False), ("2032-09-14", True), ("2032-09-01", True),
                               ("2032-08-31", False)):
            v = self.get(as_of, "late_fee_risk|TX-US-2032-0700")
            self.assertEqual(v is not None and v.verdict == "finding", present, as_of)
        v = self.get("2032-09-10", "late_fee_risk|TX-US-2032-0700")
        self.assertEqual((v.amount_usd, v.deadline, v.remedy), (D("18.60"), "2032-09-15", "payment_reminder"))

    def test_risk_principal_less_what_we_dispute(self):
        self.w.waiver("2032-08-01T09:00:00Z", SIGNE, ["TE8"], ["Delivery fee"])
        self.w.bill("TX-US-2032-0700", "2032-08-12T09:00:00Z", [
            L("Delivery fee", "121.81", awb="TE8", qty="1", unit="shipment")], total="1000.00")
        self.w.register("2032-08-20T09:00:00Z", [("TX-US-2032-0700", "2032-08-12", "2032-09-15", "1000.00", "open",
                                                  None)])
        v = self.get("2032-09-10", "late_fee_risk|TX-US-2032-0700")
        self.assertEqual(v.amount_usd, D("16.33"))   # 1.86% of 1000.00 - 121.81


# ---- cashback --------------------------------------------------------------------------------------------------
class Cashback(Base):
    def setUp(self):
        super().setUp()
        self.w.papers(rules={
            "volume_rebate": {"applicable": True, "period": "quarter", "credit_due_days": 45,
                              "tiers": [{"threshold": "55000.00", "rate_pct": "5"},
                                        {"threshold": "91000.00", "rate_pct": "7.5"}]},
            "qualifying_spend": {"included": ["base_freight", "fuel", "security", "annex_d_accessorials"],
                                 "credit_notes": "net_all_dated_in_period"}},
            aliases={"DUTY": ["Import duty (disbursement)", "Duty paid on your behalf"]})
        self.w.bill("TX-US-2032-5462", "2032-04-12T09:00:00Z", [L("Air freight", "21517.17", awb="T1")])
        self.w.bill("TX-US-2032-5464", "2032-05-28T09:00:00Z", [L("Air freight", "44049.82", awb="T2")])
        self.w.bill("TX-US-2032-5468", "2032-07-31T09:00:00Z", [L("Air freight", "49543.66", awb="T3"),
                                                                 L("Duty paid on your behalf", "8703.54", awb="T3")])
        self.w.bill("TX-US-2032-5472", "2032-09-11T09:00:00Z", [L("Air freight", "1837.73", awb="T4"),
                                                                 L("Duty paid on your behalf", "186.96", awb="T4")])

    def test_rebate_owed_and_claimed_once_due(self):
        vs = self.w.verdicts("2032-11-30")
        v = vs["cashback|TBX-US-EXP-E60|2032-Q2|rebate"]
        self.assertEqual((v.verdict, v.amount_usd, v.remedy, v.deadline),
                         ("finding", D("3278.35"), "claim_credit", "2032-08-14"))
        self.assertEqual(vs["cashback|TBX-US-EXP-E60|2032-Q3|rebate"].verdict, "no_finding")   # duty left out

    def test_not_due_yet_is_tracked(self):
        v = self.get("2032-07-20", "cashback|TBX-US-EXP-E60|2032-Q2|rebate")
        self.assertEqual((v.verdict, v.remedy), ("finding", "track_credit"))

    def test_open_quarter_not_reported(self):
        self.assertNotIn("cashback|TBX-US-EXP-E60|2032-Q2|rebate", self.w.verdicts("2032-06-30"))

    def test_credit_already_issued(self):
        self.w.credit("2032-07-30T09:00:00Z", "CN-R2", "3278.35", "rebate", period="Q2 2032")
        self.assertEqual(self.get("2032-11-30", "cashback|TBX-US-EXP-E60|2032-Q2|rebate").verdict, "no_finding")

    def test_quarter_bounds(self):
        self.assertEqual([str(d) for d in quarter_bounds("2032-Q4")], ["2032-10-01", "2032-12-31"])


# ---- expiry ----------------------------------------------------------------------------------------------------
class Expiry(Base):
    def test_window(self):
        self.w.papers(end="2034-03-20", auto=False)
        v = self.get("2034-02-28", "expiry|TBX-US-EXP-E60")
        self.assertEqual((v.verdict, v.deadline, v.remedy), ("finding", "2034-03-20", "alert"))
        self.assertIsNone(self.get("2033-08-31", "expiry|TBX-US-EXP-E60"))
        self.assertIsNone(self.get("2034-03-21", "expiry|TBX-US-EXP-E60"))

    def test_renews_itself_so_the_notice_deadline_counts(self):
        self.w.papers(end="2034-03-20", auto=True, rules={"term": {"auto_renews": True, "non_renewal_notice_days": 90,
                                                                   "extension_by": "signed_instrument_only"}})
        self.assertEqual(self.get("2033-11-30", "expiry|TBX-US-EXP-E60").deadline, "2033-12-20")

    def test_extensions(self):
        self.w.papers(end="2034-03-20", auto=False, rules={"term": {"extension_by": "signed_instrument_only"}})
        self.w.said("2034-02-01T09:00:00Z", JOAQUIN, "contract_notice",
                    {"notice_type": "extension", "new_end_date": "2035-03-20"}, contract_id=C,
                    person_email=JOAQUIN[0])
        self.assertEqual(self.get("2034-02-28", "expiry|TBX-US-EXP-E60").deadline, "2034-03-20")  # email can't extend
        doc = self.w.email("2034-02-10T09:00:00Z", "farid.underhill@quillmere.example", "Farid", "AMD-2", "AMD-2.pdf")
        self.w.put(doc, F("instrument", {"type": "amendment", "reference": f"{C}/AMD-2", "signed_by_both": True},
                          contract_id=C),
                   F("contract_notice", {"notice_type": "extension", "new_end_date": "2035-03-20"}, contract_id=C,
                     quote="The term is extended to 2035-03-20."))
        self.assertIsNone(self.get("2034-02-28", "expiry|TBX-US-EXP-E60"))


if __name__ == "__main__":
    unittest.main()
