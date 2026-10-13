"""Tests for intake and the CLI wiring. Offline: scans are read through a fake client.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness" / "harness.py"
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "grader"))

import anthropic  # noqa: E402
import extract as grader_extract  # noqa: E402
import validate as grader_validate  # noqa: E402

from nova.intake.mail import parse_email, split_quoted  # noqa: E402
from nova.intake.mailbox import ingest_batch  # noqa: E402
from nova.intake.sheets import xlsx_pages  # noqa: E402
from nova.llm import Gateway  # noqa: E402
from nova.report.build import schema_errors  # noqa: E402
from nova.store import Store  # noqa: E402
from tests.test_pdf_vision import CANNED, FakeClient, reply, sdk_error  # noqa: E402

ALL_EMAILS = sorted(ROOT.glob("data/*/batch_*/*.eml")) + sorted(ROOT.glob("data/examples/*/docs/batch_*/*.eml"))


class GraderIdenticalText(unittest.TestCase):
    def test_every_email_page_matches_the_grader(self):
        self.assertGreater(len(ALL_EMAILS), 100)
        for f in ALL_EMAILS:
            data = f.read_bytes()
            self.assertEqual(parse_email(data).page_text, grader_extract.pages_of(data, "eml")[0], f)

    def test_every_sheet_matches_the_grader(self):
        n = 0
        for f in ALL_EMAILS:
            for name, data in grader_extract.attachments(f.read_bytes()):
                if name.lower().endswith(".xlsx"):
                    n += 1
                    self.assertEqual(xlsx_pages(data), grader_extract.xlsx_pages(data), f"{f}#{name}")
        self.assertGreater(n, 15)

    def test_vendored_report_schema_is_the_graders(self):
        ours = json.loads((ROOT / "harness/nova/report/report.schema.json").read_text())
        self.assertEqual(ours, json.loads((ROOT / "grader/schema/report.schema.json").read_text()))


class Emails(unittest.TestCase):
    def test_reply_splits_into_authored_and_quoted(self):
        em = parse_email((ROOT / "data/scenarios/batch_01/0004.eml").read_bytes())
        self.assertEqual(em.from_addr, "signe.underhill@tarrowby.example")
        self.assertEqual(em.received_at, "2034-11-19T12:16:00Z")
        self.assertIn("we waive the Delivery fee on AWB TX45998993", em.authored)
        self.assertNotIn("Could you check", em.authored)
        self.assertIn("> Could you check the Delivery fee", em.quoted)
        self.assertIn("wrote:", em.quoted)

    def test_forwards_and_header_blocks_are_quoted(self):
        for marker in ("---------- Forwarded message ---------", "-----Original Message-----",
                       "Begin forwarded message:"):
            authored, quoted = split_quoted(f"FYI, see below.\n\n{marker}\nWe waive the fee on AWB X1.")
            self.assertEqual(authored.strip(), "FYI, see below.", marker)
            self.assertIn("We waive the fee", quoted)
        authored, quoted = split_quoted("Please action.\nFrom: Signe Underhill\nSent: Monday\nTo: AP\n"
                                        "Subject: waiver\nWe waive the fee on AWB X1.")
        self.assertEqual(authored, "Please action.")
        self.assertIn("We waive the fee", quoted)

    def test_inline_reply_text_stays_authored(self):
        authored, quoted = split_quoted("> Can you waive AWB X1?\nYes, we waive it.\n> And AWB X2?\nNo.")
        self.assertEqual(authored.split("\n"), ["Yes, we waive it.", "No."])
        self.assertEqual(len(quoted.split("\n")), 2)

    def test_received_time_is_converted_to_utc(self):
        raw = (b"From: A <a@x.example>\r\nTo: b@y.example\r\nDate: Mon, 02 Jun 2035 00:30:00 +0530\r\n"
               b"Subject: t\r\n\r\nhello\r\n")
        self.assertEqual(parse_email(raw).received_at, "2035-06-01T19:00:00Z")


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(Path(self.tmp.name) / "state")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def mailbox(self, *emails: Path) -> Path:
        box = Path(self.tmp.name) / "mailbox" / "batch_01"
        box.mkdir(parents=True, exist_ok=True)
        for e in emails:
            shutil.copy(e, box / e.name)
        return box

    def test_a_worked_example_goes_in_whole_and_only_once(self):
        root = ROOT / "data/examples/duplicate/docs"
        first = {}
        for batch in sorted(root.glob("batch_*")):
            first.update(ingest_batch(self.store, batch, gateway=None))
        emls = list(root.glob("batch_*/*.eml"))
        attachments = sum(len(grader_extract.attachments(e.read_bytes())) for e in emls)
        count = lambda t: self.store.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
        self.assertEqual((count("receipts"), count("documents")), (len(emls), len(emls) + attachments))
        mb = grader_extract.Mailbox(root)
        for d in self.store.documents():
            pages = [p["text"] for p in self.store.pages(d["id"])]
            self.assertEqual(pages, mb.pages(d["doc_ref"])[0], d["doc_ref"])  # same text the grader cites against
        again = ingest_batch(self.store, sorted(root.glob("batch_*"))[0], gateway=None)
        self.assertEqual(again.get("new", 0), 0)
        self.assertEqual(count("receipts"), len(emls))

    def test_a_scan_seen_without_api_access_is_read_on_a_later_ingest(self):
        box = self.mailbox(ROOT / "data/exam/batch_02/0016.eml")
        no_access = Gateway(client=FakeClient(sdk_error(anthropic.AuthenticationError, "invalid x-api-key")))
        summary = ingest_batch(self.store, box, no_access)
        self.assertEqual(summary["documents with unread pages"], 1)
        doc = self.store.document("batch_01/0016.eml#CDI-3031-0306.pdf")
        self.assertEqual((doc["status"], doc["is_scan"]), ("new", 1))  # it is a scan, just not read yet

        summary = ingest_batch(self.store, box, Gateway(client=FakeClient(reply(CANNED))))
        self.assertEqual(summary["read on retry"], 1)
        doc = self.store.document("batch_01/0016.eml#CDI-3031-0306.pdf")
        self.assertEqual((doc["status"], doc["is_scan"]), ("read", 1))
        [page] = self.store.pages(doc["id"])
        self.assertEqual((page["source"], page["text"]), ("vision", "\n".join(CANNED)))

    def test_a_broken_email_does_not_stop_the_batch(self):
        box = self.mailbox(ROOT / "data/scenarios/batch_01/0003.eml")
        (box / "0002.eml").write_bytes(b"\x00\xff not an email at all")
        summary = ingest_batch(self.store, box, gateway=None)
        self.assertEqual(summary.get("new", 0) + summary.get("failed", 0), 2)
        self.assertIsNotNone(self.store.document("batch_01/0003.eml"))


class CommandLine(unittest.TestCase):
    """Runs harness.py the way the grader does: a fresh process, an empty working folder, a minimal environment."""

    def run_harness(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR")}
        return subprocess.run([sys.executable, str(HARNESS), *args], capture_output=True, text=True, cwd=cwd,
                              env=env, timeout=300)

    def test_the_graders_cli_check_passes(self):
        self.assertEqual(grader_validate.cli_errors(HARNESS), [])

    def test_ingest_report_feedback_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state, home = tmp / "state", tmp / "home"
            home.mkdir()
            box = ROOT / "data/examples/overcharge/docs/batch_01"
            r = self.run_harness("ingest", str(box), "--state-dir", str(state), cwd=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.run_harness("report", "--as-of", "2032-08-31", "--mode", "autonomous", "--state-dir", str(state),
                                 cwd=home)
            self.assertEqual(r.returncode, 0, r.stderr)
            report = json.loads(r.stdout)
            self.assertEqual(schema_errors(report), [])
            self.assertEqual((report["as_of"], report["mode"]), ("2032-08-31", "autonomous"))
            fb = str(ROOT / "data/scenarios/feedback/W1_batch_01.json")
            for _ in range(2):
                r = self.run_harness("feedback", fb, "--state-dir", str(state), cwd=home)
                self.assertEqual(r.returncode, 0, r.stderr)
            with Store.open(state) as store:
                runs = [(row["command"], row["status"]) for row in store.con.execute("SELECT * FROM runs")]
                self.assertEqual(runs, [("ingest", "ok"), ("report", "ok"), ("feedback", "ok"), ("feedback", "ok")])
                self.assertEqual(store.con.execute("SELECT COUNT(*) FROM feedback_items").fetchone()[0], 1)
                self.assertEqual(store.con.execute("SELECT status FROM reports").fetchone()[0], "printed")

    def test_report_on_a_missing_state_folder_still_prints_a_valid_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_harness("report", "--as-of", "2035-05-31", "--state-dir", str(Path(tmp) / "nothing"),
                                 cwd=Path(tmp))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(schema_errors(json.loads(r.stdout)), [])


if __name__ == "__main__":
    unittest.main()
