"""Three readings the exam mailbox exposed, now backed by what is printed on the page:
- an amendment read with no signatories and no effective date, although both are printed (it lost its authority);
- the late-fee clause put in the general terms list instead of the late-fee rule ("no late-fee clause");
- a house waybill (CA96669138-H1) read as its master (CA96669138), which made two shipments look like a duplicate;
- a fee line "Late fee on <invoice>" left unmapped by the model, so the late fee wasn't checked.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from nova.readers import billing, contract  # noqa: E402
from tests.test_knowledge import Base, C  # noqa: E402
from tests.test_tier1 import ctx  # noqa: E402

AMENDMENT = """Amendment No. 1 to Master Services Agreement (Express Air) CDA-US-EXP-E91
Effective from 2030-05-06
1. Annex A rates for the lanes below are deleted and replaced with the following:
USE2-USC3 0-44.5 kg 4.63 USD
All other terms of the Agreement are unchanged.
Signed for and on behalf of Ferrowyn Life Sciences Inc. (the Shipper)
Signature:
Name: Tiberius Wetherell
Title: VP Procurement
Date: 2030-04-29
Signed for and on behalf of Caldrith Air Logistics Inc. (the Carrier)
Signature:
Name: Harriet Inglethorpe
Title: VP Sales
Date: 2030-04-29"""


def terms(kind, **extra):
    return {"instrument": {"type": kind, "contract_id": "CDA-US-EXP-E91", "reference": f"CDA-US-EXP-E91/{kind}"},
            "terms": [], "doc_references": [], "schedule1": [], "automation_instructions": [], **extra}


class ThePageFillsWhatTheReadingMissed(unittest.TestCase):
    def test_signatures_and_effective_date_from_the_page(self):
        facts, _ = contract.build(ctx(AMENDMENT), terms("amendment"), {})
        [inst] = [f for f in facts if f.kind == "instrument"]
        self.assertTrue(inst.payload["signed_by_both"])
        self.assertEqual([s["name"] for s in inst.payload["signatories"]], ["Tiberius Wetherell", "Harriet Inglethorpe"])
        self.assertEqual((inst.payload["effective_from"], inst.valid_from), ("2030-05-06", "2030-05-06"))

    def test_what_the_reading_gave_is_kept(self):
        t = terms("amendment")
        t["instrument"].update(effective_from="2030-06-01",
                               signatories=[{"party": "shipper", "name": "A"}, {"party": "carrier", "name": "B"}])
        facts, _ = contract.build(ctx(AMENDMENT), t, {})
        [inst] = [f for f in facts if f.kind == "instrument"]
        self.assertEqual(([s["name"] for s in inst.payload["signatories"]], inst.payload["effective_from"]),
                         (["A", "B"], "2030-06-01"))

    def test_the_late_fee_clause_from_the_general_terms_list(self):
        page = "10.2 Late payment. Overdue amounts bear a late fee of 1.49% per month or part month, compounding."
        t = terms("msa", rules={}, terms=[{"name": "late_fee", "quote": page,
                                           "value": {"rate_pct": "1.49%", "grace_days": 0, "compounding": True}}])
        facts, _ = contract.build(ctx(page), t, {})
        [rule] = [f.payload["value"] for f in facts if f.payload.get("term") == "rule:late_fee"]
        self.assertEqual((str(rule["rate_pct"]), rule["grace_days"], rule["compounding"]), ("1.49", 0, True))

    def test_a_house_waybill_printed_on_the_line(self):
        quote = "USE2-INS2 CA96669138-H1 Delivery fee (MAWB CA96669138) 1 shpt 52.70 52.70 26/09/2030"
        data = {"doc_kind": "invoice", "number": "CDU-3051304", "contract_ref": "CDA-US-EXP-E91", "currency": "USD",
                "lines": [{"awb": "CA96669138", "label": "Delivery fee (MAWB CA96669138)", "lane": "USE2-INS2",
                           "pickup_date": "2030-09-26", "quantity": "1 shpt", "rate": "52.70", "amount": "52.70",
                           "quote": quote}],
                "automation_instructions": []}
        facts, _ = billing.build(ctx("INVOICE CDU-3051304\n" + quote), data)
        [line] = [f for f in facts if f.kind == "invoice_line"]
        self.assertEqual(line.keys["awb"], "CA96669138-H1")



class AFeeOnAnInvoice(Base):
    """Exam run 2: "Late fee on CDU-3051262: USD 1448.48 overdue, month 1" was left unmapped by the model, so the fee
    that wasn't owed went unchecked. The charge the contract lists, then the invoice it's on, maps without a model."""
    def test_the_charge_before_on_an_invoice(self):
        terms = self.w.terms()
        self.assertEqual(terms.charge_code(C, "Delivery fee on TX-US-2032-0300: USD 930.00 overdue, month 1"), "DLV")
        self.assertIsNone(terms.charge_code(C, "Delivery fee on weekends"))
        self.assertIsNone(terms.charge_code(C, "Linehaul on TX-US-2032-0300"))


if __name__ == "__main__":
    unittest.main()
