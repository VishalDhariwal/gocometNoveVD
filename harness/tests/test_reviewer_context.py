"""Tests for the reviewer's leaner context, the general reader's notes reaching it, and confidence that follows the
reading (the changes after the first practice run).

- the brief shows documents as cards and only the pages at the centre of the case; read_document gives one page at a
  time and says how many there are;
- a note from a document of no standard form that names one of our invoices becomes a reviewer case;
- a guard on findings: an overcharge on a lane a contract change names, with nothing read from it for that lane, goes
  to the reviewer, and its cannot_determine (quoting that change) replaces code's finding;
- confidence: an overcharge starts below a duplicate, and an unsettled review lowers it.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from nova.decide import confidence  # noqa: E402
from nova.decide.common import Verdict  # noqa: E402
from nova.knowledge.terms import Source, Terms  # noqa: E402
from nova.knowledge.view import View  # noqa: E402
from nova.review.library import Library  # noqa: E402
from tests.test_knowledge import F  # noqa: E402
from tests.test_knowledge import C  # noqa: E402
from tests.test_report import INV as OVER_INV, items  # noqa: E402
from tests.test_review import AWB, INV, OPS, Base, cite, decide  # noqa: E402

CARD = ("A letter from the carrier's operations desk about three shipments. It names no invoice. It quotes no rates "
        "and no amounts. Nothing in it changes what is billed or owed.")
PAGES = ["first page words about the shipments", "second page words with the delivery fee", "third page words"]


class OnePageAtATime(Base):
    def library(self, as_of="2032-07-31"):
        return Library(self.w.store, Terms(View.load(self.w.store, as_of)))

    def long_letter(self, *facts):
        ref = self.mail("2032-07-02T09:00:00Z", OPS, "Long letter", "the letter", *facts)
        doc = self.w.store.document(ref)["id"]
        self.w.store.set_pages(doc, [{"page_no": i + 1, "text": t, "source": "text"} for i, t in enumerate(PAGES)])
        self.w.store.update_document(doc, summary=CARD)
        return ref

    def test_read_document_gives_one_page_and_says_how_many(self):
        ref = self.long_letter()
        lib = self.library()
        first = lib.read_document(ref)
        self.assertIn(PAGES[0], first)
        self.assertNotIn(PAGES[1], first)
        self.assertIn("page 1 of 3", first)
        self.assertIn(CARD, first)                                   # the card heads every read
        second = lib.read_document(ref, 2)
        self.assertIn(PAGES[1], second)
        self.assertNotIn(PAGES[0], second)
        self.assertIn("It is the last page", lib.read_document(ref, 3))
        self.assertIn("has no page 7", lib.read_document(ref, 7))

    def test_the_brief_shows_the_card_and_only_the_pages_at_the_centre(self):
        ref = self.long_letter(F("document_note", {"kind": "waiver", "description": "delivery fee waived"},
                                 awb=AWB, page=2, quote="second page words with the delivery fee"))
        lib = self.library()
        d = lib.by_ref[ref]
        note = next(f for f in lib.facts_by_doc[d.doc_id] if f.kind == "document_note")
        text = lib.centre_text(d, [note.id])
        self.assertIn(CARD, text)
        self.assertIn(PAGES[1], text)
        self.assertNotIn(PAGES[0], text)
        self.assertNotIn(PAGES[2], text)
        self.assertIn("2 more page(s)", text)
        card_only = lib.centre_text(d, [note.id], card_only="the invoice")
        self.assertIn(CARD, card_only)
        self.assertNotIn(PAGES[1], card_only)

    def test_a_document_with_no_card_is_shown_from_its_first_page(self):
        ref = self.long_letter()
        self.w.store.update_document(self.w.store.document(ref)["id"], summary=None)
        lib = self.library()
        text = lib.centre_text(lib.by_ref[ref], [])
        self.assertIn(PAGES[0], text)
        self.assertNotIn(PAGES[1], text)


class GeneralNotesReachTheReviewer(Base):
    def test_a_note_naming_our_waybill_becomes_a_case(self):
        self.bill_delivery()
        body = "DELIVERY NOTE\nThe delivery fee on TE97049901 is waived as agreed with your team."
        self.mail("2032-07-02T09:00:00Z", OPS, "Delivery note", body,
                  F("document_note", {"kind": "waiver", "description": "delivery fee waived",
                                      "seems_to_be": "delivery note"},
                    awb=AWB, quote="The delivery fee on TE97049901 is waived"))
        briefs = []

        def script(text, turn, results):
            briefs.append(text)
            return [decide(verdict="no_finding", finding_class="none")]
        self.report("2032-07-31", script=script)
        self.assertTrue(any("has no standard form" in b and "waiver: delivery fee waived" in b for b in briefs))
        self.assertIn(f"open|{INV}|document_notes", [r["finding_key"] for r in self.reviews()])

    def test_an_identifiers_note_alone_is_not_a_case(self):
        self.bill_delivery()
        self.mail("2032-07-02T09:00:00Z", OPS, "List", f"Shipments: {AWB}",
                  F("document_note", {"kind": "identifiers", "awbs": [AWB]}, quote=f"Shipments: {AWB}"))
        self.report("2032-07-31")
        self.assertNotIn(f"open|{INV}|document_notes", [r["finding_key"] for r in self.reviews()])


class UncoveredLane(Base):
    """An overcharge on a lane that a contract change names, with nothing read from it for that lane: the change may
    replace those rates with a document we don't hold (practice run 3, invoice TX-US-2035-0237)."""
    AMD = ("AMENDMENT No. 7. The Annex A rates for lane(s) USE2-INE2 are deleted and replaced as follows: "
           "Annex A rev 2 (attached) applies from 2031-12-01 to lane(s) USE2-INE2.")
    CITES = "Annex A rev 2 (attached) applies from 2031-12-01 to lane(s) USE2-INE2"

    def build(self, names_the_lane=True):
        self.w.invoice(OVER_INV, "2032-01-03T09:00:00Z", [
            ("Air freight", "TE56915687", "USE2-INE2", "2031-12-25", "241", "kg", "6.69", "1612.29"),
            ("Fuel surcharge", "TE56915687", "USE2-INE2", "2031-12-25", "1612.29", None, "20.16%", "325.04")])
        self.w.tracking("2032-02-29T09:00:00Z", [("TE56915687", "PICKED_UP", "2031-12-25", "-")])
        self.amd = self.w.amendment(f"{C}/AMD-7", "2031-11-20T09:00:00Z", "2031-12-01",
                                    rates=[("USW1-USC3", "100-299.5 kg", "2.88")])
        self.w.store.update_document(self.amd, doc_type="amendment")
        self.amd_ref = self.w.store.document(self.amd)["doc_ref"]
        self.amd_text = self.AMD if names_the_lane else "AMENDMENT No. 7. USW1-USC3 100-299.5 kg 2.88 USD"

    def pages(self):
        super().pages()
        self.w.store.set_pages(self.amd, [{"page_no": 1, "text": self.amd_text, "source": "text"}])

    def test_the_reviewer_can_turn_it_into_cannot_determine(self):
        self.build()

        def script(text, turn, results):
            self.assertIn("names the lane of these lines", text)
            self.assertIn("Annex A rev 2 (attached)", text)               # the amendment's page is in the brief
            return [decide(verdict="cannot_determine", finding_class="overcharge",
                           missing_document={"name": "Annex A rev 2", "holder": "rates"},
                           citations=[cite(self.amd_ref, self.CITES, proves="the lane's rates are replaced")])]
        it = items(self.report("2032-08-31", script=script))[f"overcharge:{OVER_INV}"]
        self.assertEqual((it["verdict"], it["action"], it["missing_doc"]["name"]),
                         ("cannot_determine", "draft:request_document", "Annex A rev 2"))
        self.assertIn(f"overcharge|{OVER_INV}", [r["finding_key"] for r in self.reviews()])

    def test_no_case_when_the_change_doesnt_name_the_lane(self):
        self.build(names_the_lane=False)
        it = items(self.report("2032-08-31", script=lambda *a: self.fail("no case expected")))
        self.assertEqual(it[f"overcharge:{OVER_INV}"]["verdict"], "finding")

    def test_a_quote_from_elsewhere_cant_drop_the_finding(self):
        self.build()
        script = lambda *a: [decide(verdict="cannot_determine", finding_class="overcharge",  # noqa: E731
                                    missing_document={"name": "Annex A rev 2", "holder": "rates"}, citations=[])]
        it = items(self.report("2032-08-31", script=script))[f"overcharge:{OVER_INV}"]
        self.assertEqual(it["verdict"], "finding")                      # seat belt 5 still holds


class EmptyEmails(Base):
    """Practice runs 1-4: our AP manager's "could you check the delivery fee?" went to the reviewer every time."""
    def empty_email(self, status):
        doc = self.w.email("2032-07-02T09:00:00Z", *OPS, "Question")
        body = f"Could you check the Delivery fee on AWB {AWB}? We did not expect it."
        self.w.store.set_email(doc, from_addr=OPS[0], from_name=OPS[1], authored_text=body, subject="Question")
        self.bodies[doc] = body
        ext = self.w.store.begin_extraction(doc, "correspondence", "t1")
        self.w.store.finish_extraction(ext, status)

    def test_a_clean_read_with_nothing_in_it_is_not_a_case(self):
        self.bill_delivery()
        self.empty_email("ok")
        self.report("2032-07-31")
        self.assertNotIn(f"open|{INV}|unread_email", [r["finding_key"] for r in self.reviews()])

    def test_a_partial_read_with_nothing_in_it_still_is(self):
        self.bill_delivery()
        self.empty_email("partial")
        self.report("2032-07-31")
        self.assertIn(f"open|{INV}|unread_email", [r["finding_key"] for r in self.reviews()])


class ConfidenceFollowsTheReading(unittest.TestCase):
    def verdict(self, cls, doc_id=7):
        fact = SimpleNamespace(id=1, doc_id=doc_id, kind="rate", verification="verified", quote="a rate as printed",
                               payload={})
        v = Verdict(cls, {"invoice_id": "TX-US-2035-0001"}, "finding")
        v.evidence.append(Source(fact, 1, "the rate"))
        return v

    def test_an_overcharge_starts_below_a_duplicate(self):
        self.assertLess(confidence.score(None, self.verdict("overcharge")),
                        confidence.score(None, self.verdict("duplicate")))

    def test_an_unsettled_review_lowers_it(self):
        v = self.verdict("duplicate")
        self.assertLess(confidence.score(None, v, unsettled=True), confidence.score(None, v))


if __name__ == "__main__":
    unittest.main()
