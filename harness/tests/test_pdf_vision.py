"""Offline tests for the scanned-PDF path. No API key or network needed: a fake client stands in for Claude.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import base64
import importlib.util
import sys
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from nova.intake.pdf import TOOL, number_agreement, numeric_tokens, ocr_numbers, read_pdf  # noqa: E402
from nova.llm import Gateway  # noqa: E402
from nova.store import Store  # noqa: E402

SCAN = (ROOT / "data/exam/batch_02/0016.eml", "CDI-3031-0306.pdf")
TEXT = (ROOT / "data/exam/batch_01/0005.eml", "CDU-3051254.pdf")
CANNED = [
    "Charges advice CDI-3031-0306",
    "Route | Waybill | Charge | Basis | Rate | Amount | Collected",
    "INE2-USW1 | CA68214550 | Air freight | 274 kg | 330.42 | 90,535.08 | 06/01/2031",
]


def attachment(eml: Path, name: str) -> bytes:
    msg = BytesParser(policy=policy.default).parsebytes(eml.read_bytes())
    return next(p.get_content() for p in msg.iter_attachments() if p.get_filename() == name)


class FakeClient:
    """Plays back a script of replies (or exceptions) and records every request."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Exploding:
    """A client that fails the test if it's ever called."""

    @property
    def messages(self):
        raise AssertionError("the API was called, but the answer should have come from the cache")


def reply(lines=None, *, stop="tool_use", data=None, model="fake-model"):
    block = SimpleNamespace(type="tool_use", id="toolu_1", name=TOOL["name"],
                            input=data if data is not None else {"lines": lines, "notes": ""})
    return SimpleNamespace(content=[block], stop_reason=stop, model=model,
                           usage=SimpleNamespace(input_tokens=1500, output_tokens=200))


def sdk_error(cls, message: str):
    """An SDK exception instance without building an HTTP response."""
    e = cls.__new__(cls)
    Exception.__init__(e, message)
    e.message = message
    return e


class TextPages(unittest.TestCase):
    def test_text_layer_matches_the_grader_exactly(self):
        sys.path.insert(0, str(ROOT / "grader"))
        from extract import pdf_pages
        data = attachment(*TEXT)
        pages = read_pdf(data, gateway=None)
        self.assertEqual([p.source for p in pages], ["text"])
        self.assertEqual([p.text for p in pages], pdf_pages(data))


class ScannedPages(unittest.TestCase):
    def setUp(self):
        self.scan = attachment(*SCAN)

    def test_scan_goes_to_vision_with_a_forced_tool_call(self):
        fake = FakeClient(reply(CANNED))
        pages = read_pdf(self.scan, Gateway(client=fake), models=("m1",), ocr_check=False)
        self.assertEqual(len(pages), 1)
        p = pages[0]
        self.assertEqual((p.source, p.model, p.text), ("vision", "fake-model", "\n".join(CANNED)))
        self.assertTrue(p.numbers_trusted)
        req = fake.requests[0]
        self.assertEqual(req["tool_choice"], {"type": "tool", "name": "record_transcription"})
        image = req["messages"][0]["content"][0]
        self.assertEqual(image["source"]["media_type"], "image/png")
        png = base64.b64decode(image["source"]["data"])
        self.assertTrue(png.startswith(b"\x89PNG"))
        pix = pymupdf.Pixmap(png)
        self.assertEqual(max(pix.width, pix.height), 1568)

    def test_cache_makes_a_rerun_free_and_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Store.open(tmp) as store:
                first = read_pdf(self.scan, Gateway(store.cache, client=FakeClient(reply(CANNED))),
                                 models=("m1",), ocr_check=False)
            with Store.open(tmp) as store:  # a fresh process in real life: only the file carries over
                again = read_pdf(self.scan, Gateway(store.cache, client=Exploding()), models=("m1",),
                                 ocr_check=False)
        self.assertEqual(first[0].text, again[0].text)
        self.assertTrue(again[0].cached)

    def test_falls_back_to_the_next_model(self):
        fake = FakeClient(sdk_error(anthropic.NotFoundError, "model: missing-model not found"), reply(CANNED))
        p = read_pdf(self.scan, Gateway(client=fake), models=("missing-model", "m2"), ocr_check=False)[0]
        self.assertEqual(p.source, "vision")
        self.assertEqual([r["model"] for r in fake.requests], ["missing-model", "m2"])

    def test_cut_off_answer_is_retried_with_more_room(self):
        fake = FakeClient(reply([], stop="max_tokens"), reply(CANNED))
        p = read_pdf(self.scan, Gateway(client=fake), models=("m1",), ocr_check=False)[0]
        self.assertEqual(p.source, "vision")
        self.assertEqual([r["max_tokens"] for r in fake.requests], [4096, 8192])

    def test_answer_that_breaks_the_schema_gets_one_repair_round(self):
        fake = FakeClient(reply(data={"notes": ""}), reply(CANNED))
        p = read_pdf(self.scan, Gateway(client=fake), models=("m1",), ocr_check=False)[0]
        self.assertEqual(p.source, "vision")
        repair = fake.requests[1]["messages"]
        self.assertEqual(len(repair), 3)
        self.assertTrue(repair[2]["content"][0]["is_error"])

    def test_no_api_access_degrades_instead_of_crashing(self):
        fake = FakeClient(sdk_error(anthropic.AuthenticationError, "invalid x-api-key"))
        p = read_pdf(self.scan, Gateway(client=fake), models=("m1", "m2"), ocr_check=False)[0]
        self.assertEqual((p.source, p.text), ("unread", ""))
        self.assertIn("credentials rejected", p.error)
        self.assertEqual(len(fake.requests), 1)  # a rejected key isn't retried on every model
        p = read_pdf(self.scan, gateway=None)[0]
        self.assertEqual(p.source, "unread")

    def test_unreadable_marks_lower_trust(self):
        fake = FakeClient(reply(["Total payable | INR 127,0[?]4.44"]))
        p = read_pdf(self.scan, Gateway(client=fake), models=("m1",), ocr_check=False)[0]
        self.assertEqual(p.unreadable_marks, 1)
        self.assertFalse(p.numbers_trusted)


class Numbers(unittest.TestCase):
    def test_numeric_tokens(self):
        self.assertEqual(numeric_tokens("INR127,024.44 | 274kg | 25.33% | 06/01/2031 | TX27216277 | No. 4"),
                         ["127024.44", "274", "25.33", "2031", "27216277"])

    def test_agreement_counts_repeats(self):
        self.assertEqual(number_agreement(["274", "274", "330.42"], ["274", "330.42"]), (0.6667, ["274"]))
        self.assertEqual(number_agreement([], ["1"]), (1.0, []))


@unittest.skipUnless(importlib.util.find_spec("rapidocr_onnxruntime"), "rapidocr not installed")
class OcrCrossCheck(unittest.TestCase):
    def test_ocr_reads_the_key_numbers_on_the_exam_scan(self):
        with pymupdf.open(stream=attachment(*SCAN), filetype="pdf") as doc:
            nums = ocr_numbers(doc[0])
        for n in ("90535.08", "22932.54", "127024.44", "68214550"):
            self.assertIn(n, nums)


if __name__ == "__main__":
    unittest.main()
