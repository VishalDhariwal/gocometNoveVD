"""Tests for tools/llm_proxy.py, the local Anthropic-to-OpenAI stand-in used to test the harness on an OpenAI key.

OpenAI itself is faked; everything else is real: the proxy's HTTP server, the Anthropic SDK pointed at it with
ANTHROPIC_BASE_URL, and our gateway on top.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import llm_proxy as px  # noqa: E402

from nova.llm import Gateway, LLMFailed, LLMUnavailable  # noqa: E402

TOOL = {"name": "record_answer", "description": "Record it.",
        "input_schema": {"type": "object", "properties": {
            "total": {"type": "string"}, "kind": {"enum": ["a", "b"]}, "note": {"type": ["string", "null"]}},
            "required": ["total", "kind"]}}
MINI = "gpt-4o-mini"
HEADERS = {"x-ratelimit-limit-requests": "500", "x-ratelimit-limit-tokens": "200000",
           "x-ratelimit-remaining-tokens": "199000", "x-ratelimit-reset-tokens": "1s"}


def reply(name="record_answer", args=None, finish="tool_calls", raw=None, text=None, cached=0):
    calls = [] if args is None and raw is None else [
        {"id": "call_1", "type": "function", "function": {"name": name, "arguments": raw or json.dumps(args)}}]
    return ({"choices": [{"message": {"role": "assistant", "content": text, "tool_calls": calls},
                          "finish_reason": finish}],
             "usage": {"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240,
                       "prompt_tokens_details": {"cached_tokens": cached}}}, dict(HEADERS))


class FakeOpenAI(px.OpenAI):
    def __init__(self, replies, **kw):
        kw.setdefault("strong", "gpt-4.1-mini")
        super().__init__("test-key", fast=MINI, retries=3, timeout=5, **kw)
        self.replies, self.sent = list(replies), []

    def post(self, payload):
        self.sent.append(json.loads(json.dumps(payload)))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Translation(unittest.TestCase):
    def test_request(self):
        body = {"model": "claude-sonnet-5-5", "max_tokens": 900,
                "system": [{"type": "text", "text": "Review.", "cache_control": {"type": "ephemeral"}}],
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "read this"},
                                                 {"type": "image", "source": {"type": "base64",
                                                                              "media_type": "image/png", "data": "AAA"}}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "Looking."},
                                                      {"type": "tool_use", "id": "t1", "name": "record_answer",
                                                       "input": {"total": 5}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                                  "content": [{"type": "text", "text": "total must be a string"}]},
                                                 {"type": "text", "text": "Try again."}]}],
                "tools": [TOOL], "tool_choice": {"type": "tool", "name": "record_answer"}}
        o = px.to_openai(body, model=MINI, max_tokens=900)
        self.assertEqual([m["role"] for m in o["messages"]], ["system", "user", "assistant", "tool", "user"])
        self.assertEqual(o["messages"][0]["content"], "Review.")
        self.assertEqual(o["messages"][1]["content"][1],
                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}})
        self.assertEqual(o["messages"][2], {"role": "assistant", "content": "Looking.", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "record_answer", "arguments": '{"total": 5}'}}]})
        self.assertEqual(o["messages"][3], {"role": "tool", "tool_call_id": "t1",
                                            "content": "Error: total must be a string"})
        self.assertEqual(o["messages"][4], {"role": "user", "content": "Try again."})
        self.assertEqual((o["tool_choice"], o["parallel_tool_calls"]),
                         ({"type": "function", "function": {"name": "record_answer"}}, False))
        self.assertEqual(o["tools"][0]["function"]["parameters"], TOOL["input_schema"])
        self.assertEqual((o["temperature"], o["max_completion_tokens"]), (0, 900))
        any_ = px.to_openai({**body, "tool_choice": {"type": "any"}}, model="o4-mini", max_tokens=10)
        self.assertEqual(any_["tool_choice"], "required")
        self.assertNotIn("temperature", any_)            # reasoning models take no temperature
        self.assertNotIn("parallel_tool_calls", any_)

    def test_response(self):
        msg = px.from_openai(reply(raw='```json\n{"total": "5.00", "kind": "a"}\n```', cached=128)[0], MINI)
        self.assertEqual((msg["stop_reason"], msg["content"], msg["usage"]),
                         ("tool_use", [{"type": "tool_use", "id": "call_1", "name": "record_answer",
                                        "input": {"total": "5.00", "kind": "a"}}],
                          {"input_tokens": 200, "cache_read_input_tokens": 128, "output_tokens": 40}))
        cut = px.from_openai(reply(raw='{"total": "5.0', finish="length")[0], MINI)
        self.assertEqual(cut["stop_reason"], "max_tokens")
        with self.assertRaises(px.BadToolCall):
            px.from_openai(reply(raw="{not json")[0], MINI)

    def test_errors(self):
        e = px.UpstreamError(400, "This model's maximum context length is 128000 tokens.")
        self.assertEqual(px.anthropic_error(e), (400, "This LLM's maximum context length is 128000 tokens."))
        self.assertEqual(px.anthropic_error(px.UpstreamError(404, "The model `x` does not exist", code="model_not_found"))[0], 404)
        self.assertEqual(px.anthropic_error(px.UpstreamError(503, "busy"))[0], 529)
        self.assertTrue(px._no_capacity(px.UpstreamError(429, "You exceeded your current quota", code="insufficient_quota")))
        self.assertFalse(px._no_capacity(px.UpstreamError(429, "Rate limit reached for requests", code="rate_limit_exceeded")))
        self.assertEqual(px.UpstreamError(429, "x", headers={"retry-after-ms": "250"}).retry_after, 0.25)

    def test_durations(self):
        self.assertEqual([px._duration(x) for x in ("7.66s", "1m2.5s", "340ms", "2", None)],
                         [7.66, 62.5, 0.34, 2.0, None])
        self.assertEqual(px._try_again_in("Please try again in 1m30s. Visit ..."), 90.0)

    def test_window(self):
        w = px.Window(rpm=2, tpm=1000)
        w.acquire(100, 100)
        w.acquire(100, 100)
        self.assertGreater(w.wait_needed(100, time.monotonic()), 50)          # a third request in the minute waits
        w = px.Window(rpm=0, tpm=1000)
        e = w.acquire(900, 900)
        self.assertGreater(w.wait_needed(300, time.monotonic()), 50)          # 900 + 300 > 1000
        w.settle(e, 200, {})                                                  # it really used 200
        self.assertLessEqual(w.wait_needed(300, time.monotonic()), 0)
        w.settle(None, 0, {"x-ratelimit-limit-tokens": "12000", "x-ratelimit-limit-requests": "60",
                           "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "6s"})
        self.assertEqual((w.tpm, w.rpm), (12000, 60))
        self.assertAlmostEqual(w.wait_needed(6000, time.monotonic()), 3.0, delta=0.3)   # half refilled after 3 s
        w.settle(None, 0, {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "4s"})
        self.assertGreater(w.wait_needed(1, time.monotonic()), 3.5)

    def test_answer_capped_to_fit_the_minute(self):
        o = FakeOpenAI([reply(args={"total": "1", "kind": "a"})], tpm=6000)
        o.messages({"model": "claude-sonnet-5-5", "max_tokens": 16000,
                    "messages": [{"role": "user", "content": "x" * 9000}],
                    "tools": [TOOL], "tool_choice": {"type": "tool", "name": "record_answer"}})
        self.assertLess(o.sent[0]["max_completion_tokens"] + px.estimate_tokens(o.sent[0]), 6000)


class ThroughTheSdk(unittest.TestCase):
    """The real Anthropic SDK and our gateway, talking to the proxy over HTTP."""

    def start(self, replies, **kw):
        backend = FakeOpenAI(replies, **kw)
        server = px.serve(backend, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        patcher = mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
                                               "ANTHROPIC_API_KEY": "local-proxy"})
        patcher.start()
        self.addCleanup(patcher.stop)
        return backend, Gateway(max_retries=0)

    def ask(self, gw, models=("claude-sonnet-5-5",), max_tokens=500):
        return gw.call_tool(purpose="t", version="1", models=list(models), system="You read invoices.",
                            content=[{"type": "text", "text": "read this"}], tool=TOOL, max_tokens=max_tokens)

    def test_round_trip_and_rate_limit_retry(self):
        busy = px.UpstreamError(429, "Rate limit reached for gpt-4o-mini on tokens per min (TPM). Please try again "
                                     "in 10ms.", code="rate_limit_exceeded", headers={"retry-after-ms": "10"})
        backend, gw = self.start([busy, reply(args={"total": "5.00", "kind": "a"})])
        res = self.ask(gw)
        self.assertEqual((res.data, res.model, gw.input_tokens), ({"total": "5.00", "kind": "a"}, MINI, 200))
        self.assertEqual(len(backend.sent), 2)

    def test_repair_round_goes_through(self):
        backend, gw = self.start([reply(args={"total": 5, "kind": "a"}), reply(args={"total": "5.00", "kind": "a"})])
        self.assertEqual(self.ask(gw).data["total"], "5.00")
        self.assertEqual([m["role"] for m in backend.sent[1]["messages"]], ["system", "user", "assistant", "tool"])

    def test_cut_off_answer_asked_again_with_more_room(self):
        backend, gw = self.start([reply(raw='{"total": "5', finish="length"), reply(args={"total": "5", "kind": "a"})])
        self.assertEqual(self.ask(gw, max_tokens=500).data["total"], "5")
        self.assertEqual([s["max_completion_tokens"] for s in backend.sent], [500, 1000])

    def test_no_credit_means_no_api(self):
        broke = px.UpstreamError(429, "You exceeded your current quota.", code="insufficient_quota")
        _, gw = self.start([broke, broke])
        with self.assertRaises(LLMUnavailable):
            self.ask(gw, models=("claude-opus-5-5", "claude-sonnet-5-5"))

    def test_bad_key_means_no_api(self):
        _, gw = self.start([px.UpstreamError(401, "Incorrect API key provided.", code="invalid_api_key")])
        with self.assertRaises(LLMUnavailable):
            self.ask(gw)

    def test_unknown_model_falls_back_to_the_next(self):
        backend, gw = self.start([px.UpstreamError(404, "The model `gpt-4.1-mini` does not exist",
                                                   code="model_not_found"),
                                  reply(args={"total": "1.00", "kind": "b"})])
        res = self.ask(gw, models=("claude-opus-5-5", "claude-sonnet-5-5"))
        self.assertEqual(([s["model"] for s in backend.sent], res.data["kind"]), (["gpt-4.1-mini", MINI], "b"))

    def test_plain_bad_request_is_not_a_missing_model(self):
        _, gw = self.start([px.UpstreamError(400, "This model's maximum context length is 128000 tokens.")])
        with self.assertRaises(LLMFailed):
            self.ask(gw, models=("claude-opus-5-5", "claude-sonnet-5-5"))

    def test_arguments_not_json_asked_again(self):
        backend, gw = self.start([reply(raw="{not json"), reply(args={"total": "8.00", "kind": "a"})])
        self.assertEqual(self.ask(gw).data["total"], "8.00")
        self.assertEqual(len(backend.sent), 2)

    def test_cache_and_call_log(self):
        with tempfile.TemporaryDirectory() as d:
            log_file = Path(d) / "calls.jsonl"
            backend, gw = self.start([reply(args={"total": "1.00", "kind": "a"}, cached=64)], cache_dir=d,
                                     call_log=str(log_file))
            self.ask(gw)
            again = Gateway(max_retries=0)          # a new run: the harness's own cache is empty
            self.assertEqual(self.ask(again).data["total"], "1.00")
            self.assertEqual(len(backend.sent), 1)
            rows = [json.loads(x) for x in log_file.read_text().splitlines()]
            self.assertEqual([r["status"] for r in rows], ["ok", "cached"])
            self.assertEqual((rows[0]["tool"], rows[0]["input"], rows[0]["cached_input"], rows[0]["output"]),
                             ("record_answer", 200, 64, 40))
            self.assertIn("api_seconds", rows[0])

    def test_agent_conversation(self):
        backend, gw = self.start([reply("search_documents", {"text": "TE97"}),
                                  reply(args={"total": "1", "kind": "a"})])
        tools = [TOOL, {**TOOL, "name": "search_documents"}]
        msgs = [{"role": "user", "content": [{"type": "text", "text": "case file"}]}]
        first = gw.step(purpose="t", version="1", models=["claude-opus-5-5"], system="Review.", messages=msgs,
                        tools=tools, max_tokens=500, cache_system=True)
        msgs += [{"role": "assistant", "content": first.content},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": first.calls[0]["id"],
                                               "content": "2 hits"}]}]
        second = gw.step(purpose="t", version="1", models=["claude-opus-5-5"], system="Review.", messages=msgs,
                         tools=tools, max_tokens=500)
        self.assertEqual(second.calls[0]["input"], {"total": "1", "kind": "a"})
        sent = backend.sent[1]
        self.assertEqual(sent["model"], "gpt-4.1-mini")
        self.assertEqual([m["role"] for m in sent["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(sent["messages"][3], {"role": "tool", "tool_call_id": "call_1", "content": "2 hits"})
        self.assertEqual(sent["tool_choice"], "required")


if __name__ == "__main__":
    unittest.main()
