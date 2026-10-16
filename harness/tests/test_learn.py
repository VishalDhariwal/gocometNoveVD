"""Tests for phase 6: feedback applied inside its scope, and learned_at only where it changed the answer.

Each case mirrors a kind in GLOSSARY "feedback.json" (and the practice set's fb-01-p1), with its sibling that the
feedback must NOT reach.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from nova import cli  # noqa: E402
from nova.report.build import schema_errors  # noqa: E402
from tests.test_decide import JOAQUIN, OUTSIDER, SIGNE, L, Mailbox  # noqa: E402
from tests.test_knowledge import C, FINTAN  # noqa: E402
from tests.test_report import items, paginate  # noqa: E402

AFTER = "2032-06-02T10:15:00Z"


def fb(item_id, decision, key, fact, scope):
    return {"item_id": item_id, "decision": decision, "finding_key": key, "fact": fact, "scope": scope}


def one_off(invoice_id, valid_from="2032-06-02"):
    return {"kind": "one_off", "applies_to": {"invoice_id": invoice_id}, "valid_from": valid_from, "valid_to": None}


class Learn(unittest.TestCase):
    def setUp(self):
        self.w = Mailbox()
        self.w.msa()

    def tearDown(self):
        self.w.close()

    def report(self, as_of, mode="supervised"):
        paginate(self.w)
        rep = cli.report(as_of, mode, self.w.tmp.name)
        self.assertEqual(schema_errors(rep), [])
        return items(rep)

    def overcharged(self, number, received, pickup="2031-12-25", awb="TE56915687"):
        self.w.invoice(number, received, [
            ("Air freight", awb, "USE2-INE2", pickup, "241", "kg", "6.69", "1612.29"),
            ("Fuel surcharge", awb, "USE2-INE2", pickup, "1612.29", None, "20.16%", "325.04")])


class RateAccepted(Learn):
    """The practice set's fb-01-p1: a notice from someone without rates authority, accepted for one lane."""

    def setUp(self):
        super().setUp()
        self.w.notice("2032-01-28T09:00:00Z", JOAQUIN, [("USC3-INS1", "45-99.5 kg", "5.03"),
                                                         ("USC3-INS1", "100-299.5 kg", "4.79")],
                      "2032-02-01", "2032-09-25")
        self.w.notice("2032-01-29T09:00:00Z", JOAQUIN, [("USE2-INW2", "100-299.5 kg", "5.86")], "2032-02-01")
        self.w.invoice("TX-US-2032-0210", "2032-04-20T09:00:00Z", [
            ("Air freight", "TX13648356", "USC3-INS1", "2032-04-10", "91", "kg", "5.03", "457.73")])
        self.w.feedback([fb("fb-01-p1", "close", {"invoice_id": "TX-US-2032-0210", "class": "overcharge"},
                            {"type": "rate_accepted", "subject": {"contract_id": C, "lane": "USC3-INS1"},
                             "value": {"source": "rate notice", "sent_by": "Joaquin Rathbone",
                                       "effective_from": "2032-02-01", "effective_to": "2032-09-25"}},
                            {"kind": "standing", "applies_to": {"contract_id": C, "lane": "USC3-INS1"},
                             "valid_from": "2032-02-01", "valid_to": "2032-09-25"})], decided_at=AFTER)
        self.w.invoice("TX-US-2032-0239", "2032-07-01T09:00:00Z", [
            ("Air freight", "TX77231778", "USC3-INS1", "2032-06-25", "103", "kg", "4.79", "493.37")])
        self.w.invoice("TX-US-2032-0256", "2032-08-25T09:00:00Z", [
            ("Air freight", "TX99107819", "USC3-INS1", "2032-08-20", "120", "kg", "5.10", "612.00")])
        self.w.invoice("TX-US-2032-0259", "2032-08-27T09:00:00Z", [
            ("Air freight", "TX43529111", "USE2-INW2", "2032-08-22", "172", "kg", "5.86", "1007.92")])

    def test_before_and_after(self):
        before = self.report("2032-05-31")
        self.assertEqual(before["overcharge:TX-US-2032-0210"]["amount_usd"], "29.12")
        after = self.report("2032-09-30")
        self.assertNotIn("overcharge:TX-US-2032-0210", after)          # closed
        self.assertNotIn("overcharge:TX-US-2032-0239", after)          # billed at the accepted rate: fine
        changed = after["overcharge:TX-US-2032-0256"]
        self.assertEqual((changed["amount_usd"], changed["learned_at"]),
                         ("37.20", {"feedback_item_id": "fb-01-p1", "batch": "batch_01"}))
        other_lane = after["overcharge:TX-US-2032-0259"]
        self.assertEqual((other_lane["amount_usd"], other_lane["learned_at"]), ("53.32", None))


class SenderAuthority(Learn):
    def setUp(self):
        super().setUp()
        self.w.waiver("2032-06-10T09:00:00Z", JOAQUIN, ["TE1"], ["Delivery fee"])
        self.w.waiver("2032-06-10T09:00:00Z", OUTSIDER, ["TE2"], ["Delivery fee"])
        for n, awb in (("TX-US-2032-0301", "TE1"), ("TX-US-2032-0302", "TE2")):
            self.w.bill(n, "2032-06-20T09:00:00Z", [L("Delivery fee", "121.81", awb=awb, lane="USE2-INE2",
                                                      pickup="2032-06-12", qty="1", unit="shipment")])

    def grant(self, category, categories):
        self.w.feedback([fb("fb-03-01", "confirm", {"invoice_id": "TX-US-2032-0301", "class": "waived_but_billed"},
                            {"type": "sender_authority", "subject": {"person": "Joaquin Rathbone", "party": "TBX-US"},
                             "value": {"categories": categories}},
                            {"kind": "standing", "applies_to": {"contract_id": C, "person": "Joaquin Rathbone",
                                                                "category": category},
                             "valid_from": "2032-06-01", "valid_to": None})], decided_at="2032-07-01T10:00:00Z")

    def test_confirmed_person_now_counts(self):
        self.assertNotIn("waived_but_billed:TX-US-2032-0301", self.report("2032-06-30"))
        self.grant("waivers", ["waivers"])
        after = self.report("2032-07-31")
        it = after["waived_but_billed:TX-US-2032-0301"]
        self.assertEqual((it["amount_usd"], it["learned_at"]["feedback_item_id"]), ("121.81", "fb-03-01"))
        self.assertNotIn("waived_but_billed:TX-US-2032-0302", after)   # someone else: not covered

    def test_another_category_doesnt_count(self):
        self.grant("rates", ["rates"])
        self.assertNotIn("waived_but_billed:TX-US-2032-0301", self.report("2032-07-31"))


class KeyDecisions(Learn):
    def test_close_stops_that_key_only(self):
        self.overcharged("TX-US-2032-5394", "2032-01-03T09:00:00Z")
        self.w.feedback([fb("fb-02-02", "close", {"invoice_id": "TX-US-2032-5394", "class": "overcharge"},
                            {"type": "dispute_closed", "subject": {"invoice_id": "TX-US-2032-5394"},
                             "value": {"status": "closed"}}, one_off("TX-US-2032-5394"))], decided_at=AFTER)
        self.overcharged("TX-US-2032-5400", "2032-07-03T09:00:00Z", awb="TE70000001")   # same wrong rate, later
        before, after = self.report("2032-05-31"), self.report("2032-07-31")
        self.assertIn("overcharge:TX-US-2032-5394", before)
        self.assertNotIn("overcharge:TX-US-2032-5394", after)
        self.assertEqual(after["overcharge:TX-US-2032-5400"]["amount_usd"], "196.92")

    def test_reject_drops_the_key(self):
        self.overcharged("TX-US-2032-5394", "2032-01-03T09:00:00Z")
        self.w.feedback([fb("fb-02-09", "reject", {"invoice_id": "TX-US-2032-5394", "class": "overcharge"},
                            {"type": "dispute_closed"}, one_off("TX-US-2032-5394"))], decided_at=AFTER)
        self.assertNotIn("overcharge:TX-US-2032-5394", self.report("2032-07-31"))

    def test_override_amount(self):
        self.overcharged("TX-US-2032-5394", "2032-01-03T09:00:00Z")
        self.w.feedback([fb("fb-02-07", "override_amount", {"invoice_id": "TX-US-2032-5394", "class": "overcharge"},
                            {"type": "dispute_closed", "value": {"status": "settled", "amount_usd": "150.00"}},
                            one_off("TX-US-2032-5394"))], decided_at=AFTER)
        it = self.report("2032-07-31")["overcharge:TX-US-2032-5394"]
        self.assertEqual((it["amount_usd"], it["learned_at"]["feedback_item_id"]), ("150.00", "fb-02-07"))

    def test_feedback_that_changes_nothing_isnt_learned(self):
        self.overcharged("TX-US-2032-5394", "2032-01-03T09:00:00Z")
        self.w.feedback([fb("fb-02-08", "override_amount", {"invoice_id": "TX-US-2032-5394", "class": "overcharge"},
                            {"type": "dispute_closed", "value": {"status": "settled", "amount_usd": "196.92"}},
                            one_off("TX-US-2032-5394"))], decided_at=AFTER)
        it = self.report("2032-07-31")["overcharge:TX-US-2032-5394"]
        self.assertEqual((it["amount_usd"], it["learned_at"]), ("196.92", None))


class WaiverScope(Learn):
    """A waiver that names no month, approved for October on one statement: November is not waived."""

    def setUp(self):
        super().setUp()
        self.w.waiver("2032-09-20T09:00:00Z", SIGNE, [], ["Monthly account fee"])
        for n, month in (("TX-ST-203210", "2032-10"), ("TX-ST-203211", "2032-11")):
            self.w.bill(n, f"{month}-28T09:00:00Z", [L("Monthly account fee", "350.28", qty="1", unit="month",
                                                       rate="350.28", period=month)])

    def test_one_off_approval_covers_that_key_only(self):
        self.assertNotIn("waived_but_billed:TX-ST-203210", self.report("2032-11-30"))
        self.w.feedback([fb("fb-02-04", "one_off_approval", {"invoice_id": "TX-ST-203210",
                                                             "class": "waived_but_billed"},
                            {"type": "waiver_scope", "subject": {"person": "Signe Underhill", "party": "TBX-US"},
                             "value": {"months": ["2032-10"]}}, one_off("TX-ST-203210", "2032-11-30"))],
                        decided_at="2032-12-01T10:00:00Z")
        after = self.report("2032-12-31")
        it = after["waived_but_billed:TX-ST-203210"]
        self.assertEqual((it["amount_usd"], it["learned_at"]["feedback_item_id"]), ("350.28", "fb-02-04"))
        self.assertNotIn("waived_but_billed:TX-ST-203211", after)    # applying it to November caps the score


class ResolveConflict(Learn):
    def test_conflict_becomes_a_finding(self):
        self.w.amendment(f"{C}/AMD-1", "2032-03-16T09:00:00Z", "2032-03-16",
                         rates=[("USW1-USC3", "100-299.5 kg", "2.88")])
        self.w.notice("2032-06-02T09:00:00Z", FINTAN, [("USW1-USC3", "100-299.5 kg", "3.03")], "2032-06-01")
        self.w.invoice("TX-US-2032-5166", "2032-06-26T09:00:00Z", [
            ("Air freight", "TE92942629", "USW1-USC3", "2032-06-17", "113", "kg", "3.03", "342.39"),
            ("Fuel surcharge", "TE92942629", "USW1-USC3", "2032-06-17", "342.39", None, "23.14%", "79.23")])
        self.assertEqual(self.report("2032-06-30")["overcharge:TX-US-2032-5166"]["verdict"], "conflict")
        self.w.feedback([fb("fb-02-06", "resolve_conflict", {"invoice_id": "TX-US-2032-5166", "class": "overcharge"},
                            {"type": "precedence", "subject": {"contract_id": C, "lane": "USW1-USC3"},
                             "value": {"use": "amendment", "amendment": f"{C}/AMD-1"}},
                            {"kind": "standing", "applies_to": {"contract_id": C, "lane": "USW1-USC3"},
                             "valid_from": "2032-06-01", "valid_to": None})], decided_at="2032-07-01T10:00:00Z")
        it = self.report("2032-07-31")["overcharge:TX-US-2032-5166"]
        self.assertEqual((it["verdict"], it["amount_usd"], it["learned_at"]["feedback_item_id"], it["supersedes"]),
                         ("finding", "20.87", "fb-02-06", ["overcharge:TX-US-2032-5166"]))


if __name__ == "__main__":
    unittest.main()
