"""Tests for the SQLite state store. Offline; each test gets its own temp state folder.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from nova import normalize as norm  # noqa: E402
from nova.llm import Gateway  # noqa: E402
from nova.store import MIGRATIONS_LATEST, Store, StoreError  # noqa: E402
from tests.test_pdf_vision import CANNED, TOOL, Exploding, FakeClient, reply  # noqa: E402

FEEDBACK = ROOT / "data/scenarios/feedback/W1_batch_01.json"


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def doc(self, path="batch_01/0001.eml", received="2035-05-31T23:59:59Z", data=b"raw email"):
        rid, _ = self.store.add_receipt(path=path, batch=path.split("/")[0], data=data, message_id="<m@x>",
                                        received_at=received)
        did, _ = self.store.add_document(doc_ref=path, receipt_id=rid, fmt="eml", data=data, received_at=received)
        return did

    def rate_fact(self, ext, rate="6.01"):
        return self.store.add_fact(ext, "rate", {"band": "100-299.5", "rate": rate, "currency": "USD"},
                                   verification="verified", contract_id=" tbx-us-exp-01", lane="use2-ine2", page=8,
                                   quote="USE2-INE2 100-299.5 kg 6.01 6.16", valid_from="2034-10-20")


class Opening(StoreTest):
    def test_new_store_is_at_the_latest_schema_and_reopens_cleanly(self):
        self.assertEqual(self.store.schema_version, MIGRATIONS_LATEST)
        self.store.close()
        self.store = Store.open(self.tmp.name)
        self.assertEqual(self.store.schema_version, MIGRATIONS_LATEST)

    def test_refuses_a_store_from_newer_code(self):
        self.store.con.execute("PRAGMA user_version = 99")
        self.store.close()
        with self.assertRaises(StoreError):
            Store.open(self.tmp.name)
        self.store = Store.open(tempfile.mkdtemp())  # so tearDown has something to close


class WhatArrived(StoreTest):
    def test_ingesting_the_same_file_twice_is_a_no_op(self):
        a = self.doc()
        b = self.doc()
        self.assertEqual(a, b)
        self.assertEqual(self.store.con.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 1)

    def test_same_path_with_different_bytes_is_refused(self):
        self.doc()
        with self.assertRaises(StoreError):
            self.doc(data=b"something else")

    def test_identical_bytes_are_stored_once(self):
        self.doc("batch_01/0001.eml", data=b"same invoice pdf")
        self.doc("batch_01/0002.eml", data=b"same invoice pdf")
        self.assertEqual(self.store.con.execute("SELECT COUNT(*) FROM blobs").fetchone()[0], 1)

    def test_email_identity_is_the_lower_cased_address(self):
        d = self.doc()
        self.store.set_email(d, from_addr=" Signe.Underhill@Tarrowby.example ", from_name="Signe Underhill",
                             authored_text="We waive the Delivery fee on AWB TX45998993.", quoted_text="> Hi,")
        self.assertEqual(self.store.email(d)["from_addr"], "signe.underhill@tarrowby.example")

    def test_pages_are_replaced_not_duplicated(self):
        d = self.doc()
        self.store.set_pages(d, [{"number": 1, "text": "first read", "source": "text"}])
        self.store.set_pages(d, [{"number": 1, "text": "second read", "source": "vision", "model": "m"}])
        self.assertEqual([p["text"] for p in self.store.pages(d)], ["second read"])


class Facts(StoreTest):
    def test_keys_are_normalised_and_filtered(self):
        ext = self.store.begin_extraction(self.doc(), "contract", "v1")
        self.rate_fact(ext)
        self.store.finish_extraction(ext, "ok")
        [f] = self.store.facts("rate", contract_id="TBX-US-EXP-01", lane="USE2-INE2")
        self.assertEqual((f.contract_id, f.lane, f.payload["rate"]), ("TBX-US-EXP-01", "USE2-INE2", "6.01"))
        self.assertEqual((f.doc_ref, f.page, f.known_at), ("batch_01/0001.eml", 8, "2035-05-31T23:59:59Z"))

    def test_facts_are_append_only(self):
        ext = self.store.begin_extraction(self.doc(), "contract", "v1")
        fid = self.rate_fact(ext)
        for sql in ("UPDATE facts SET payload = '{}' WHERE id = ?", "DELETE FROM facts WHERE id = ?"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.con.execute(sql, (fid,))

    def test_rereading_switches_current_facts_but_keeps_history(self):
        d = self.doc()
        old = self.store.begin_extraction(d, "contract", "v1")
        self.rate_fact(old, "9.99")
        self.store.finish_extraction(old, "ok")
        new = self.store.begin_extraction(d, "contract", "v2")
        self.rate_fact(new, "6.01")
        self.store.finish_extraction(new, "ok")
        self.assertEqual([f.payload["rate"] for f in self.store.facts("rate")], ["6.01"])
        self.assertEqual(self.store.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 2)

    def test_a_failed_reread_keeps_the_previous_answer(self):
        d = self.doc()
        ok = self.store.begin_extraction(d, "contract", "v1")
        self.rate_fact(ok)
        self.store.finish_extraction(ok, "ok")
        bad = self.store.begin_extraction(d, "contract", "v2")
        self.store.finish_extraction(bad, "failed", error="API down")
        self.assertEqual(len(self.store.facts("rate")), 1)
        self.assertEqual(self.store.document(d)["status"], "extracted")

    def test_no_facts_after_an_extraction_finishes(self):
        ext = self.store.begin_extraction(self.doc(), "contract", "v1")
        self.store.finish_extraction(ext, "ok")
        with self.assertRaises(StoreError):
            self.rate_fact(ext)

    def test_as_of_covers_the_whole_day_and_nothing_after(self):
        for path, received in (("batch_01/0001.eml", "2035-05-31T23:59:59Z"),
                               ("batch_01/0002.eml", "2035-06-01T00:00:00Z")):
            ext = self.store.begin_extraction(self.doc(path, received, data=path.encode()), "contract", "v1")
            self.rate_fact(ext)
            self.store.finish_extraction(ext, "ok")
        self.assertEqual(len(self.store.facts("rate", as_of="2035-05-31")), 1)
        self.assertEqual(len(self.store.facts("rate", as_of="2035-06-01")), 2)

    def test_kind_and_required_keys_are_checked(self):
        ext = self.store.begin_extraction(self.doc(), "billing", "v1")
        with self.assertRaises(StoreError):
            self.store.add_fact(ext, "made_up_kind", {}, verification="verified")
        with self.assertRaises(StoreError):
            self.store.add_fact(ext, "invoice_line", {"amount": "1.00"}, verification="verified")  # no invoice_id
        with self.assertRaises(StoreError):
            self.store.add_fact(ext, "rate", {}, verification="verified", contract_id="C", lane="L",
                                valid_from="2035-02-01", valid_to="2035-01-01")

    def test_money_is_never_a_float(self):
        ext = self.store.begin_extraction(self.doc(), "billing", "v1")
        with self.assertRaises(StoreError):
            self.store.add_fact(ext, "invoice_line", {"lines": [{"amount": 1701.31}]}, verification="verified",
                                invoice_id="X-1")
        self.store.add_fact(ext, "invoice_line", {"amount": Decimal("1701.31")}, verification="verified",
                            invoice_id="tx-us 2035#0189")
        self.store.finish_extraction(ext, "ok")
        [f] = self.store.facts("invoice_line", invoice_id="TX-US2035#0189")
        self.assertEqual((f.invoice_id, f.payload["amount"]), ("TX-US20350189", "1701.31"))

    def test_a_failed_transaction_leaves_nothing_behind(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.doc()
                raise RuntimeError("step killed halfway")
        self.assertEqual(self.store.con.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)
        self.assertEqual(self.store.con.execute("SELECT COUNT(*) FROM blobs").fetchone()[0], 0)


class Links(StoreTest):
    def test_a_link_is_only_visible_once_its_inputs_have_arrived(self):
        self.store.put_link("label_code", "TBX-US-EXP-01|screeningfeeadj", "SEC", method="exact", confidence=1.0,
                            known_at="2035-03-01T00:00:00Z")
        self.store.put_link("label_code", "TBX-US-EXP-01|screeningfeeadj", "SEC", method="exact", confidence=1.0,
                            known_at="2035-03-01T00:00:00Z")  # same again: updated, not duplicated
        self.assertEqual(self.store.links("label_code", as_of="2035-02-28"), {})
        self.assertEqual(self.store.links("label_code", as_of="2035-03-01")["TBX-US-EXP-01|screeningfeeadj"]["object"],
                         "SEC")


class Feedback(StoreTest):
    def test_practice_feedback_is_stored_once_and_counts_from_decided_at(self):
        data = FEEDBACK.read_bytes()
        _, new = self.store.add_feedback_file(str(FEEDBACK), data)
        _, again = self.store.add_feedback_file(str(FEEDBACK), data)
        self.assertEqual((new, again), (True, False))
        self.assertEqual(self.store.feedback_items(as_of="2035-06-01"), [])
        [item] = self.store.feedback_items(as_of="2035-09-30")
        self.assertEqual((item["finding_key"], item["decision"], item["fact_type"], item["scope_kind"]),
                         ("overcharge|TX-US-2035-0210", "close", "rate_accepted", "standing"))
        self.assertEqual(item["applies_to"], {"contract_id": "TBX-US-EXP-01", "lane": "USC3-INS1"})


class Reports(StoreTest):
    def item(self, rid, amount, version=1):
        self.store.record_item(rid, finding_key="overcharge|X-1", item_id=f"overcharge:X-1~{version}",
                               version=version, signature=f"finding|{amount}", verdict="finding",
                               action="draft:dispute", item={"amount_usd": amount})

    def test_previous_answer_comes_from_printed_reports_only(self):
        r1 = self.store.begin_report("2035-05-31", "supervised")
        self.item(r1, "196.92")
        self.store.finish_report(r1, {"findings": []})
        r2 = self.store.begin_report("2035-06-30", "supervised")
        self.item(r2, "200.00", version=2)
        self.store.finish_report(r2, None, status="failed")
        r3 = self.store.begin_report("2035-09-30", "supervised")
        prev = self.store.last_emitted("supervised", "overcharge|X-1", before_report=r3)
        self.assertEqual((prev["version"], prev["signature"]), (1, "finding|196.92"))
        self.assertEqual(list(self.store.previous_items("supervised", before_report=r3)), ["overcharge|X-1"])
        self.assertIsNone(self.store.last_emitted("autonomous", "overcharge|X-1", before_report=r3))

    def test_the_same_outbound_action_is_never_queued_twice(self):
        rid = self.store.begin_report("2035-05-31", "autonomous")
        args = dict(idempotency_key="overcharge:X-1~1|execute:dispute", report_id=rid, mode="autonomous",
                    action="execute:dispute", finding_key="overcharge|X-1", item_id="overcharge:X-1~1",
                    subject="Dispute X-1", body="...", status="queued", recipient_email="Billing@Carrier.example")
        self.assertTrue(self.store.queue_outbound(**args))
        self.assertFalse(self.store.queue_outbound(**args))


class LlmCache(StoreTest):
    def test_gateway_answers_survive_a_fresh_process(self):
        args = dict(purpose="t", version="v1", models=("m1",), system="s",
                    content=[{"type": "text", "text": "hi"}], tool=TOOL)
        first = Gateway(self.store.cache, client=FakeClient(reply(CANNED))).call_tool(**args)
        self.store.close()
        self.store = Store.open(self.tmp.name)
        again = Gateway(self.store.cache, client=Exploding()).call_tool(**args)
        self.assertEqual((again.data, again.cached), (first.data, True))

    def test_cache_is_safe_from_worker_threads(self):
        cache = self.store.cache

        def work(i):
            cache.put(f"k{i}", {"data": {"lines": [str(i)]}, "model": "m", "purpose": "t", "version": "v1"})
            return cache.get(f"k{i}")["data"]["lines"][0]

        with ThreadPoolExecutor(8) as pool:
            self.assertEqual(sorted(pool.map(work, range(40)), key=int), [str(i) for i in range(40)])


class Normalise(unittest.TestCase):
    def test_finding_keys_match_the_grader(self):
        self.assertEqual(norm.finding_key({"invoice_id": "txi/35-36/0024 ", "class": "overcharge"}),
                         "overcharge|TXI/35-36/0024")
        self.assertEqual(norm.finding_key({"contract_id": "C1", "period": "2035-Q1", "credit_type": "rebate"}),
                         "cashback|C1|2035-Q1|rebate")
        self.assertEqual(norm.finding_key({"contract_id": "C1"}), "expiry|C1")

    def test_labels_compare_without_spaces_or_punctuation(self):
        self.assertEqual(norm.label("Delivery fee"), norm.label("Deliveryfee"))
        self.assertEqual(norm.label("Screening fee (adj.)"), "screeningfeeadj")

    def test_naive_datetimes_are_refused(self):
        from datetime import datetime
        with self.assertRaises(ValueError):
            norm.utc_iso(datetime(2035, 1, 1))


if __name__ == "__main__":
    unittest.main()
