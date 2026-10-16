"""Tests for phase 5: the action gate, confidence, citations, ids / supersedes / retro, the printed report, drafts.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import sys
import unittest
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from nova import cli  # noqa: E402
from nova.decide import policy  # noqa: E402
from nova.decide.common import Verdict  # noqa: E402
from nova.knowledge.terms import Source  # noqa: E402
from nova.report.build import schema_errors  # noqa: E402
from nova.report.evidence import Citations, norm  # noqa: E402
from tests.test_decide import JOAQUIN, Mailbox  # noqa: E402
from tests.test_knowledge import C, FINTAN, RATES  # noqa: E402

INV = "TX-US-2032-5394"


def paginate(w, skip=()):
    """Give every document page text holding its facts' quotes, as a real reading would have."""
    pages = defaultdict(lambda: defaultdict(list))
    for f in w.store.facts():
        if f.quote and f.quote not in skip:
            pages[f.doc_id][f.page or 1].append(f.quote)
    for doc_id, by_page in pages.items():
        w.store.set_pages(doc_id, [{"page_no": n, "text": "\n".join(q), "source": "text"}
                                   for n, q in sorted(by_page.items())])


def items(rep):
    out = {}
    for it in rep["findings"] + [x for v in rep["portfolio"].values() for x in v]:
        out[it["id"].split("~")[0]] = it
    return out


class World(unittest.TestCase):
    def setUp(self):
        self.w = Mailbox()
        self.w.msa()

    def tearDown(self):
        self.w.close()

    def overcharge(self, tracking=True, rate="6.69", amount="1612.29", fuel="325.04"):
        self.w.invoice(INV, "2032-01-03T09:00:00Z", [
            ("Air freight", "TE56915687", "USE2-INE2", "2031-12-25", "241", "kg", rate, amount),
            ("Fuel surcharge", "TE56915687", "USE2-INE2", "2031-12-25", amount, None, "20.16%", fuel)])
        self.w.invoice("TX-US-2032-5397", "2032-01-27T09:00:00Z", [
            ("Air freight", "TE68271803", "USE2-INE2", "2032-01-18", "44", "kg", "7.73", "340.12")])
        if tracking:
            self.w.tracking("2032-02-29T09:00:00Z", [("TE56915687", "PICKED_UP", "2031-12-25", "-"),
                                                      ("TE68271803", "PICKED_UP", "2032-01-18", "-")])

    def report(self, as_of, mode="supervised"):
        paginate(self.w)
        rep = cli.report(as_of, mode, self.w.tmp.name)
        self.assertEqual(schema_errors(rep), [])
        return rep

    def outbox(self):
        return self.w.store.con.execute("SELECT * FROM outbox ORDER BY id").fetchall()


# ---- the action gate -------------------------------------------------------------------------------------------
class Actions(World):
    def test_dispute_by_mode(self):
        self.overcharge()
        sup = items(self.report("2032-08-31"))[f"overcharge:{INV}"]
        aut = items(self.report("2032-08-31", "autonomous"))[f"overcharge:{INV}"]
        self.assertEqual((sup["action"], aut["action"]), ("draft:dispute", "execute:dispute"))
        self.assertEqual(sup["recipient"]["email"], JOAQUIN[0])        # Schedule 1 billing_disputes contact

    def test_pickup_date_from_the_invoice_isnt_enough_to_act_alone(self):
        self.overcharge(tracking=False)
        aut = items(self.report("2032-08-31", "autonomous"))[f"overcharge:{INV}"]
        self.assertEqual(aut["action"], "human_review")

    def test_5000_or_more_needs_a_person_in_both_modes(self):
        self.overcharge(rate="30.00", amount="7230.00", fuel="1457.57")
        for mode in ("supervised", "autonomous"):
            self.assertEqual(items(self.report("2032-08-31", mode))[f"overcharge:{INV}"]["action"], "human_review")

    def test_threshold_sums_every_outbound_finding_on_the_invoice(self):
        terms = self.terms()
        a = Verdict("overcharge", {"invoice_id": "X-1"}, "finding", D("4000.00"), remedy="dispute", contract=C)
        b = Verdict("duplicate", {"invoice_id": "X-1"}, "finding", D("1000.00"), remedy="dispute", contract=C)
        c = Verdict("overcharge", {"invoice_id": "X-2"}, "finding", D("4999.99"), remedy="dispute", contract=C)
        gates = policy.decide(terms, [a, b, c], "supervised")
        self.assertEqual([gates[v.finding_key].action for v in (a, b, c)],
                         ["human_review", "human_review", "draft:dispute"])

    def test_internal_actions(self):
        terms = self.terms()
        vs = [Verdict("expiry", {"contract_id": C}, "finding", remedy="alert", contract=C),
              Verdict("late_fee_risk", {"invoice_id": "X-3"}, "finding", D("18.60"), remedy="payment_reminder"),
              Verdict("cashback", {"contract_id": C, "period": "2032-Q2", "credit_type": "rebate"}, "finding",
                      D("12.00"), remedy="track_credit", contract=C),
              Verdict("overcharge", {"invoice_id": "X-4"}, "conflict", remedy="escalate", contract=C)]
        for mode in ("supervised", "autonomous"):
            gates = policy.decide(terms, vs, mode)
            self.assertEqual([gates[v.finding_key].action for v in vs],
                             ["internal:alert", "internal:payment_reminder", "internal:track_credit", "escalate"])

    def terms(self):
        from nova.knowledge.terms import Terms
        from nova.knowledge.view import View
        return Terms(View.load(self.w.store))


class Abstaining(World):
    def setUp(self):
        super().setUp()
        RATES.append(("USW1-USC3", "45-99.5 kg", "3.80", "3.90"))
        self.w.close()
        self.w = Mailbox()
        self.w.msa()
        RATES.pop()
        self.w.amendment(f"{C}/AMD-1", "2032-03-16T09:00:00Z", "2032-03-16",
                         rates=[("USW1-USC3", "100-299.5 kg", "2.88")],
                         refs=[("Annex A rev 2 (attached)", ["USS2-INN1"], "2032-06-04")])
        self.w.notice("2032-06-02T09:00:00Z", FINTAN, [("USW1-USC3", "100-299.5 kg", "3.03")], "2032-06-01")
        self.w.invoice("TX-US-2032-5166", "2032-06-26T09:00:00Z", [
            ("Air freight", "TE92942629", "USW1-USC3", "2032-06-17", "113", "kg", "3.03", "342.39"),
            ("Fuel surcharge", "TE92942629", "USW1-USC3", "2032-06-17", "342.39", None, "23.14%", "79.23")])
        self.w.invoice("TX-US-2032-5170", "2032-06-28T09:00:00Z", [   # same lane, another band, in the window
            ("Air freight", "TE1", "USW1-USC3", "2032-06-20", "50", "kg", "4.50", "225.00")])
        self.w.invoice("TX-US-2032-5171", "2032-06-28T09:00:00Z", [   # another lane
            ("Air freight", "TE2", "USE2-INE2", "2032-06-20", "150", "kg", "6.50", "975.00")])
        self.w.invoice("TX-US-2032-0237", "2032-06-20T09:00:00Z", [
            ("Air freight", "TX5", "USS2-INN1", "2032-06-10", "150", "kg", "7.79", "1168.50")])
        self.w.tracking("2032-06-30T09:00:00Z", [("TE92942629", "PICKED_UP", "2032-06-17", "-"),
                                                  ("TE1", "PICKED_UP", "2032-06-20", "-"),
                                                  ("TE2", "PICKED_UP", "2032-06-20", "-"),
                                                  ("TX5", "PICKED_UP", "2032-06-10", "-")])

    def test_conflict_missing_and_open_conflicts(self):
        sup, aut = items(self.report("2032-11-30")), items(self.report("2032-11-30", "autonomous"))
        conflict = sup["overcharge:TX-US-2032-5166"]
        self.assertEqual((conflict["verdict"], conflict["amount_range"], conflict["action"]),
                         ("conflict", ["0.00", "20.87"], "escalate"))
        self.assertGreaterEqual(len(conflict["evidence"]), 2)
        self.assertEqual(aut["overcharge:TX-US-2032-5166"]["action"], "escalate")
        missing = sup["overcharge:TX-US-2032-0237"]
        self.assertEqual((missing["action"], aut["overcharge:TX-US-2032-0237"]["action"]),
                         ("draft:request_document", "execute:request_document"))
        self.assertEqual(missing["recipient"]["person"], "Fintan Corrigan")
        self.assertEqual(aut["overcharge:TX-US-2032-5170"]["action"], "human_review")   # blocked by the open conflict
        self.assertEqual(aut["overcharge:TX-US-2032-5171"]["action"], "execute:dispute")

    def test_conflict_and_missing_say_what_a_person_must_do(self):
        found = items(self.report("2032-11-30"))
        self.assertIn("between USD 0.00 and USD 20.87", found["overcharge:TX-US-2032-5166"]["explanation"][0])
        self.assertIn("Ask Fintan Corrigan, Pricing Manager, TBX-US", found["overcharge:TX-US-2032-0237"]["explanation"][0])


# ---- citations -------------------------------------------------------------------------------------------------
class Citing(World):
    def test_quotes_are_checked_moved_or_dropped(self):
        self.overcharge()
        paginate(self.w)
        rate = next(f for f in self.w.store.facts("rate") if f.quote == "USE2-INE2 100-299.5 kg 6.01 6.16")
        checker = Citations(self.w.store)
        self.assertEqual(checker.check(Source(rate, 1, "")), {"doc": rate.doc_ref, "page": 8, "quote": rate.quote})
        moved = rate.__class__(**{**rate.__dict__, "page": 3})
        self.assertEqual(checker.check(Source(moved, 1, ""))["page"], 8)        # found on another page
        wrong = rate.__class__(**{**rate.__dict__, "quote": "USE2-INE2 100-299.5 kg 9.99 9.99"})
        self.assertIsNone(checker.check(Source(wrong, 1, "")))
        short = rate.__class__(**{**rate.__dict__, "quote": "6.01 6.16"})
        self.assertIsNone(checker.check(Source(short, 1, "")))                    # under 12 characters
        self.w.store.update_document(rate.doc_id, is_scan=1)
        scan = Citations(self.w.store)
        self.assertIsNotNone(scan.check(Source(wrong, 1, "")))                    # a scan: page number only

    def test_printed_quotes_are_on_their_pages(self):
        self.overcharge()
        rep = self.report("2032-08-31")
        for it in rep["findings"]:
            for e in it["evidence"]:
                doc = self.w.store.document(e["doc"])
                page = {p["page_no"]: p["text"] for p in self.w.store.pages(doc["id"])}[e["page"]]
                self.assertIn(norm(e["quote"]), norm(page))

    def test_lost_citations_lower_confidence_and_stop_execution(self):
        self.overcharge()
        good = items(self.report("2032-08-31", "autonomous"))[f"overcharge:{INV}"]
        for doc in self.w.store.documents():
            self.w.store.set_pages(doc["id"], [{"page_no": 1, "text": "nothing useful here at all", "source": "text"}])
        rep = cli.report("2032-08-31", "autonomous", self.w.tmp.name)
        bad = items(rep)[f"overcharge:{INV}"]
        self.assertLess(bad["confidence"], good["confidence"])
        self.assertEqual(bad["action"], "human_review")


# ---- ids, supersedes, retro, learned_at, drafts --------------------------------------------------------------------
class History(World):
    def test_ids_supersedes_and_retro(self):
        self.overcharge()
        first = items(self.report("2032-08-31"))
        self.assertEqual(first[f"overcharge:{INV}"]["id"], f"overcharge:{INV}")
        self.assertNotIn("overcharge:TX-US-2032-5397", first)
        again = items(self.report("2032-08-31"))
        self.assertEqual(again[f"overcharge:{INV}"]["id"], f"overcharge:{INV}")    # same answer, same id
        self.w.amendment(f"{C}/AMD-9", "2032-09-05T09:00:00Z", P1_START,           # backdated, both bands
                         rates=[("USE2-INE2", "100-299.5 kg", "5.90"), ("USE2-INE2", "0-44.5 kg", "7.50")])
        later = items(self.report("2032-09-30"))
        changed = later[f"overcharge:{INV}"]
        self.assertEqual((changed["id"], changed["supersedes"], changed["amount_usd"]),
                         (f"overcharge:{INV}~2", [f"overcharge:{INV}"], "228.77"))
        new = later["overcharge:TX-US-2032-5397"]
        self.assertEqual((new["amount_usd"], new.get("retro"), new["supersedes"]), ("10.12", True, []))
        self.assertFalse(changed.get("retro", False))

    def test_drafts_once_and_queue_in_autonomous(self):
        self.overcharge()
        self.report("2032-08-31")
        self.report("2032-08-31")
        rows = self.outbox()
        self.assertEqual([(r["action"], r["status"]) for r in rows], [("draft:dispute", "drafted")])
        self.assertEqual(rows[0]["recipient_email"], JOAQUIN[0])
        draft = Path(self.w.tmp.name) / "drafts" / "supervised" / f"overcharge_{INV}.txt"
        text = draft.read_text()
        self.assertIn("USD 196.92", text)
        self.assertIn(f"To: Joaquin Rathbone <{JOAQUIN[0]}>", text)
        self.report("2032-08-31", "autonomous")
        self.assertEqual([(r["mode"], r["status"]) for r in self.outbox()][-1], ("autonomous", "queued"))

    def test_learned_at_from_feedback(self):
        self.w.notice("2032-01-28T09:00:00Z", JOAQUIN, [("USC3-INS1", "100-299.5 kg", "4.79")], "2032-02-01",
                      "2032-09-25")
        self.w.invoice("TX-US-2032-0256", "2032-08-25T09:00:00Z", [
            ("Air freight", "TX99107819", "USC3-INS1", "2032-08-20", "120", "kg", "5.10", "612.00")])
        self.w.feedback([{"item_id": "fb-01-p1", "decision": "close",
                          "finding_key": {"invoice_id": "TX-US-2032-0210", "class": "overcharge"},
                          "fact": {"type": "rate_accepted", "subject": {"contract_id": C, "lane": "USC3-INS1"},
                                   "value": {"sent_by": "Joaquin Rathbone", "effective_from": "2032-02-01"}},
                          "scope": {"kind": "standing", "applies_to": {"contract_id": C, "lane": "USC3-INS1"},
                                    "valid_from": "2032-02-01", "valid_to": "2032-09-25"}}])
        it = items(self.report("2032-09-30"))["overcharge:TX-US-2032-0256"]
        self.assertEqual((it["amount_usd"], it["learned_at"]),
                         ("37.20", {"feedback_item_id": "fb-01-p1", "batch": "batch_01"}))

    def test_confidence_ranks_weaker_evidence_lower(self):
        self.overcharge()
        strong = items(self.report("2032-08-31"))[f"overcharge:{INV}"]["confidence"]
        w2 = Mailbox()
        self.addCleanup(w2.close)
        w2.msa()
        self.w, keep = w2, self.w
        self.overcharge(tracking=False)
        weak = items(self.report("2032-08-31"))[f"overcharge:{INV}"]["confidence"]
        self.w = keep
        self.assertTrue(0.05 <= weak < strong <= 0.98)


class Explaining(World):
    def test_explanation_and_evidence_roles(self):
        self.overcharge()
        it = items(self.report("2032-08-31"))[f"overcharge:{INV}"]
        why = it["explanation"]
        self.assertEqual(why[0], f"Invoice {INV} was billed USD 196.92 above the contract price.")
        for step in ("Air freight (FRT) on TE56915687: billed 1612.29, should be 1448.41.", "6.01 x 241 kg = 1448.41",
                     "20.16% x 1448.41 = 292.00", "Pickup date of TE56915687: 2031-12-25, from the tracking export",
                     "Total billed above the contract price: 196.92 USD"):
            self.assertTrue(any(line.startswith(step) for line in why), step)
        roles = {e["quote"]: e["role"] for e in it["evidence"]}
        self.assertEqual(roles["USE2-INE2 100-299.5 kg 6.01 6.16"], "contract rate (Annex A)")
        self.assertEqual(roles["TE56915687 | PICKED_UP | 2031-12-25 | USE2 | -"], "pickup date (tracking)")
        self.assertIn("billed line", roles.values())
        self.assertIn("contract clause", roles.values())


P1_START = "2031-12-01"

if __name__ == "__main__":
    unittest.main()
