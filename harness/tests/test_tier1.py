"""Tests for the Tier 1 robustness changes:

- the valid part of an answer is kept instead of losing the document (llm/salvage.py, the gateway);
- rate periods matched loosely ("Period P1" is "P1"), so rates keep their dates (readers/contract.py);
- identifiers we already hold, found in the text when a reader left them out (links.py, correspondence, late fees);
- every document gets a reader: the general one for types we don't know (readers/general.py, readers/run.py);
- no silent picks: two rates for the same date from one document are a "reading" conflict, and pricing claims only
  what is billed above every reading (knowledge/terms.py, decide/overcharge.py).

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from nova.decide.adjudicate import adjudicate  # noqa: E402
from nova.decide.late_fees import named_invoice  # noqa: E402
from nova.knowledge.terms import Conflict  # noqa: E402
from nova.knowledge.view import View  # noqa: E402
from nova.links import find_known  # noqa: E402
from nova.llm import Gateway  # noqa: E402
from nova.llm.salvage import only_filled_lists, salvage  # noqa: E402
from nova.readers import contract as K  # noqa: E402
from nova.readers import correspondence as M  # noqa: E402
from nova.readers import general as G  # noqa: E402
from nova.readers.base import DocContext  # noqa: E402
from nova.readers.run import reader_for  # noqa: E402
from tests import test_knowledge as TK  # noqa: E402

SCHEMA = {"type": "object", "properties": {
    "number": {"type": "string"},
    "lines": {"type": "array", "items": {"type": "object", "properties": {
        "amount": {"type": "string"}, "unit": {"enum": ["kg", "shipment"]}}, "required": ["amount"]}},
    "fx": {"type": "array", "items": {"type": "string"}},
    "note": {"type": "string"}}, "required": ["number", "lines", "fx"]}
TOOL = {"name": "record", "description": "Record it.", "input_schema": SCHEMA}


def ctx(text: str, *, fmt="pdf", email=None, parent=None, known=frozenset()) -> DocContext:
    return DocContext(1, "batch_01/0001.eml#doc.pdf", fmt, "doc.pdf", "2035-03-13T09:00:00Z",
                      [(1, text, "email" if fmt == "eml" else "text")], None, email, parent, None,
                      known_contracts=frozenset(known))


class Model:
    """Plays the model for the gateway: the same answer (or answers in turn) to whatever it's asked."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        data = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        block = SimpleNamespace(type="tool_use", id=f"toolu_{self.calls}", name=kw["tools"][0]["name"], input=data)
        return SimpleNamespace(content=[block], stop_reason="tool_use", model="fake-model",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


# ---- keeping the valid part of an answer ------------------------------------------------------------------------
class Salvage(unittest.TestCase):
    def test_a_list_left_out_becomes_empty(self):
        data, notes = salvage({"number": "A", "lines": []}, SCHEMA)
        self.assertEqual(data, {"number": "A", "lines": [], "fx": []})
        self.assertTrue(only_filled_lists(notes))

    def test_a_broken_list_item_is_dropped_and_the_rest_kept(self):
        data, notes = salvage({"number": "A", "lines": [{"amount": "1"}, {"unit": "kg"}], "fx": []}, SCHEMA)
        self.assertEqual(data["lines"], [{"amount": "1"}])
        self.assertTrue(any(n.startswith("dropped lines/1") for n in notes))
        self.assertFalse(only_filled_lists(notes))

    def test_a_wrong_optional_value_is_dropped(self):
        data, _ = salvage({"number": "A", "lines": [{"amount": "1", "unit": "box"}], "fx": [], "note": 5}, SCHEMA)
        self.assertEqual(data, {"number": "A", "lines": [{"amount": "1"}], "fx": []})

    def test_a_missing_required_value_at_the_top_cant_be_saved(self):
        data, notes = salvage({"lines": [], "fx": []}, SCHEMA)
        self.assertIsNone(data)
        self.assertTrue(notes)


class GatewayKeepsTheValidPart(unittest.TestCase):
    def ask(self, model):
        return Gateway(client=model, max_retries=0).call_tool(
            purpose="t", version="1", models=["m"], system="s", content=[{"type": "text", "text": "x"}], tool=TOOL)

    def test_lists_the_document_has_nothing_for_cost_no_second_call(self):
        model = Model({"number": "A", "lines": []})
        res = self.ask(model)
        self.assertEqual((res.data["fx"], model.calls), ([], 1))
        self.assertTrue(only_filled_lists(res.notes))

    def test_after_the_repair_rounds_the_valid_part_is_kept(self):
        model = Model({"number": "A", "lines": [{"amount": "1"}, {"unit": "kg"}], "fx": []})
        res = self.ask(model)                 # no LLMFailed: one broken line doesn't lose the document
        self.assertEqual(model.calls, 3)
        self.assertEqual(res.data["lines"], [{"amount": "1"}])
        self.assertTrue(any("dropped" in n for n in res.notes))

    def test_a_fix_in_the_repair_round_wins(self):
        model = Model({"number": "A", "lines": [{"unit": "kg"}], "fx": []},
                      {"number": "A", "lines": [{"amount": "2"}], "fx": []})
        res = self.ask(model)
        self.assertEqual((res.data["lines"], res.notes, model.calls), ([{"amount": "2"}], [], 2))


# ---- rate periods ------------------------------------------------------------------------------------------------
PAGE = """Annex A: Express rates (USD per chargeable kg; minimum charge per shipment)
Period P1: pickups 2034-10-20 to 2035-04-19
Period P2: pickups 2035-04-20 to 2035-10-19
Lane Weight band P1 rate P2 rate
USS2-INW2 100-299.5 kg 2.84 2.83
Agreement No. TBX-US-EXP-01"""


def tables(p1_label="Period P1", p2_label="Period P2: pickups 2035-04-20 to 2035-10-19"):
    return {"rate_periods": [{"label": p1_label, "from": "2034-10-20", "to": "2035-04-19",
                              "quote": "Period P1: pickups 2034-10-20 to 2035-04-19"},
                             {"label": p2_label, "from": "2035-04-20", "to": "2035-10-19",
                              "quote": "Period P2: pickups 2035-04-20 to 2035-10-19"}],
            "rates": [{"lane": "USS2-INW2", "band": "100-299.5 kg", "band_kind": "per_kg", "band_from": "100",
                       "band_to": "299.5", "values": [{"period": "P1", "rate": "2.84"}, {"period": "P2", "rate": "2.83"}],
                       "quote": "USS2-INW2 100-299.5 kg 2.84 2.83"}],
            "fuel": [], "accessorials": [], "charge_codes": [], "fx": [], "automation_instructions": []}


TERMS = {"instrument": {"type": "msa", "contract_id": "TBX-US-EXP-01", "effective_from": "2034-10-20",
                        "quote": "Agreement No. TBX-US-EXP-01"},
         "terms": [], "schedule1": [], "automation_instructions": []}


class RatePeriods(unittest.TestCase):
    def test_period_names_match_loosely(self):
        self.assertEqual({K.period_key(x) for x in ("P1", "Period P1", "P1 rate", "Period P1: pickups 2034-10-20")},
                         {"p1"})
        periods = {"Period P1": ("a", "b"), "Period P2": ("c", "d")}
        self.assertEqual(K.match_periods(periods, "P2"), ("c", "d"))
        self.assertIsNone(K.match_periods(periods, "P3"))

    def test_rates_keep_their_dates_when_the_names_differ(self):
        facts, problems = K.build(ctx(PAGE), TERMS, tables())
        rates = sorted((f for f in facts if f.kind == "rate"), key=lambda f: f.payload["period_label"])
        self.assertEqual([(f.payload["rate"], f.valid_from, f.valid_to) for f in rates],
                         [(D("2.84"), "2034-10-20", "2035-04-19"), (D("2.83"), "2035-04-20", "2035-10-19")])
        self.assertFalse([p for p in problems if "unknown period" in p])

    def test_a_period_that_cant_be_placed_is_named(self):
        self.assertEqual(K.unknown_periods(tables(p1_label="Phase A", p2_label="Phase B")), ["P1", "P2"])
        self.assertEqual(K.unknown_periods(tables()), [])


# ---- identifiers we already hold --------------------------------------------------------------------------------
class KnownIdentifiers(unittest.TestCase):
    def test_whole_tokens_only(self):
        known = {"TBX-US-EXP-01", "TX-US-2035-0202"}
        self.assertEqual(find_known("Reference: TBX-US-EXP-01/RN-USC3-INS1-2035-03-13", known), ["TBX-US-EXP-01"])
        self.assertEqual(find_known("late fee on tx-us-2035-0202: overdue", known), ["TX-US-2035-0202"])
        self.assertEqual(find_known("TX-US-2035-02021 and XTBX-US-EXP-01", known), [])

    def test_a_rate_notice_without_its_agreement_is_linked_to_the_one_named_in_its_reference(self):
        body = ("Reference: TBX-US-EXP-01/RN-USC3-INS1-2035-03-13\n"
                "Under clause 3.5 we give notice of the following rates for lane USC3-INS1, effective 2035-03-13 to "
                "2035-09-25:\nUSC3-INS1 45-99.5 kg: 5.03 USD")
        email = {"from_addr": "joaquin.rathbone@tarrowby.example", "subject": "Rate notice USC3-INS1",
                 "authored_text": body}
        data = {"intents": [{"type": "rate_notice", "reference": "TBX-US-EXP-01/RN-USC3-INS1-2035-03-13",
                             "effective_from": "2035-03-13", "effective_to": "2035-09-25",
                             "quote": "Under clause 3.5 we give notice of the following rates for lane USC3-INS1",
                             "rates": [{"lane": "USC3-INS1", "band": "45-99.5 kg", "band_kind": "per_kg",
                                        "band_from": "45", "band_to": "99.5", "rate": "5.03", "currency": "USD",
                                        "quote": "USC3-INS1 45-99.5 kg: 5.03 USD"}]}],
                "automation_instructions": []}
        c = ctx(body, fmt="eml", email=email, known={"TBX-US-EXP-01", "TBX-IN-EXP-01"})
        facts, problems = M.build(c, data, email["from_addr"])
        rates = [f for f in facts if f.kind == "rate"]
        self.assertEqual([f.keys["contract_id"] for f in rates], ["TBX-US-EXP-01"])
        self.assertTrue(any("taken from the email's own words" in p for p in problems))
        # without it among the agreements we hold, nothing is guessed
        facts, problems = M.build(ctx(body, fmt="eml", email=email), data, email["from_addr"])
        self.assertEqual([f for f in facts if f.kind == "rate"], [])
        self.assertIn("a rate notice that doesn't say which agreement it's under", problems)

    def test_a_late_fee_line_names_the_invoice_it_charges_for(self):
        invoices = [SimpleNamespace(invoice_id="TX-US-2035-0215"), SimpleNamespace(invoice_id="TX-US-2035-0202")]
        view = SimpleNamespace(kind=lambda k: invoices if k == "invoice" else [])
        line = SimpleNamespace(payload={"label": "Late fee",
                                        "reference": "Late fee on TX-US-2035-0202: USD 683.82 overdue, month 1"},
                               quote="14 - - Late fee on TX-US-2035-0202: USD 683.82 overdue, month 1 @ 1.98% 13.54")
        self.assertEqual(named_invoice(view, line, "TX-US-2035-0215"), "TX-US-2035-0202")
        two = SimpleNamespace(payload={"label": "Late fee", "reference": "TX-US-2035-0202 and TX-US-2035-0215"},
                              quote=None)
        self.assertIsNone(named_invoice(view, two, "TX-US-2035-9999"))   # two named: not ours to choose


# ---- no document left behind ------------------------------------------------------------------------------------
class EveryDocumentIsRead(unittest.TestCase):
    def test_routing(self):
        self.assertEqual([reader_for("pdf", t) for t in (None, "other", "invoice", "msa")],
                         ["general", "general", "billing", "contract"])
        self.assertEqual((reader_for("eml", None), reader_for("xlsx", None)), ("correspondence", "tables"))

    def test_the_general_reader_keeps_events_and_notes(self):
        page = ("PROOF OF DELIVERY\nShipment TX12345678 was delivered at INW2 on 2035-04-13.\n"
                "Payment of USD 500.00 received for invoice INV-9.")
        data = {"seems_to_be": "proof of delivery", "summary": "A proof of delivery.",
                "identifiers": {"invoices": ["INV-9"], "awbs": ["TX12345678"]},
                "events": [{"awb": "TX12345678", "event": "DELIVERED", "date": "2035-04-13", "station": "INW2",
                            "quote": "Shipment TX12345678 was delivered at INW2 on 2035-04-13."}],
                "items": [{"kind": "payment", "description": "payment received", "invoice": "INV-9", "amount": "500.00",
                           "currency": "USD", "quote": "Payment of USD 500.00 received for invoice INV-9."},
                          {"kind": "other", "description": "made up", "quote": "a sentence that isn't there"}],
                "automation_instructions": []}
        facts, problems = G.build(ctx(page, parent={"from_addr": "ops@tarrowby.example", "subject": "POD"}), data)
        kinds = [(f.kind, f.payload.get("kind")) for f in facts]
        self.assertIn(("status_update", None), kinds)
        self.assertIn(("document_note", "payment"), kinds)
        self.assertIn(("document_note", "identifiers"), kinds)
        event = next(f for f in facts if f.kind == "status_update")
        self.assertEqual((event.keys["awb"], event.payload["event"], event.payload["date"], event.verification),
                         ("TX12345678", "DELIVERED", "2035-04-13", "verified"))
        self.assertEqual(next(f for f in facts if f.payload.get("kind") == "payment").keys["invoice_id"], "INV-9")
        self.assertTrue(any("isn't on the page" in p for p in problems))   # the made-up item isn't kept


# ---- no silent picks ----------------------------------------------------------------------------------------------
OPEN = ("2031-12-01", None)


class NoSilentPicks(unittest.TestCase):
    """The MSA's two rate columns read without their period dates: both rates in force on every date."""

    def setUp(self):
        self.w = TK.World()
        with mock.patch.object(TK, "P1", OPEN), mock.patch.object(TK, "P2", OPEN):
            self.w.msa()

    def tearDown(self):
        self.w.close()

    def verdict(self, number):
        verdicts = adjudicate(View.load(self.w.store, "2032-08-31"))
        return next(v for v in verdicts if v.cls == "overcharge" and v.key.get("invoice_id") == number)

    def test_two_rates_for_one_date_are_undecided_not_picked(self):
        r = self.w.terms("2032-08-31").rate(TK.C, "USE2-INE2", date(2031, 12, 25), weight=D("241"))
        self.assertIsInstance(r, Conflict)
        self.assertEqual((r.kind, sorted(o.value for o in r.options)), ("reading", [D("6.01"), D("6.16")]))

    def test_only_what_is_billed_above_every_reading_is_claimed(self):
        self.w.invoice("TX-US-2032-5394", "2032-01-03T09:00:00Z", [
            ("Air freight", "TE56915687", "USE2-INE2", "2031-12-25", "241", "kg", "6.69", "1612.29"),
            ("Fuel surcharge", "TE56915687", "USE2-INE2", "2031-12-25", "1612.29", None, "20.16%", "325.04")])
        v = self.verdict("TX-US-2032-5394")
        # P1 6.01: 163.88 + 33.04 = 196.92 over; P2 6.16: 127.73 + 25.75 = 153.48 over -> claim 153.48
        self.assertEqual((v.verdict, v.amount_native), ("finding", D("153.48")))
        self.assertEqual(v.checks["undecided"]["most"], "196.92")

    def test_held_when_one_reading_finds_nothing(self):
        self.w.invoice("TX-US-2032-5400", "2032-01-03T09:00:00Z", [
            ("Air freight", "TE11112222", "USC3-INS1", "2031-12-20", "91", "kg", "4.71", "428.61")])
        v = self.verdict("TX-US-2032-5400")
        self.assertEqual(v.verdict, "no_finding")      # billed at the P1 rate: under P2 it would look overcharged
        self.assertIn("undecided", v.checks)
        self.assertTrue(v.why.startswith("held"))


if __name__ == "__main__":
    unittest.main()
