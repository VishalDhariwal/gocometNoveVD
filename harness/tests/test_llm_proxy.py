"""Tests for tools/llm_proxy.py, the local Anthropic-to-Gemini stand-in used for testing without an Anthropic key.

Gemini itself is faked; everything else is real: the proxy's HTTP server, the Anthropic SDK pointed at it with
ANTHROPIC_BASE_URL, and our gateway on top.

Run from the repo root:
    PYTHONPATH=harness python -m unittest discover -s harness/tests -t harness -v
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "tools"))

import llm_proxy as px  # noqa: E402

from nova.llm import Gateway, LLMUnavailable  # noqa: E402

TOOL = {"name": "record_answer", "description": "Record it.",
        "input_schema": {"type": "object", "properties": {
            "total": {"type": "string"}, "kind": {"enum": ["a", "b"]}, "note": {"type": ["string", "null"]},
            "extra": {"type": "object"}, "rows": {"type": "array", "items": {"type": "integer", "minimum": 0}}},
            "required": ["total", "kind"]}}


def gemini_reply(args=None, finish="STOP", text=None):
    parts = [{"text": "thinking...", "thought": True}]
    parts += [{"functionCall": {"name": "record_answer", "args": args}}] if args is not None else []
    parts += [{"text": text}] if text else []
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 30, "thoughtsTokenCount": 5}}


class FakeGemini(px.Gemini):
    def __init__(self, replies):
        super().__init__("test-key", fast="gemini-2.5-flash", strong="gemini-2.5-pro", thinking=0, rpm=0,
                         retries=0, timeout=5)
        self.replies, self.sent = list(replies), []

    def post(self, model, payload):
        self.sent.append((model, payload))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Translation(unittest.TestCase):
    def test_request(self):
        body = {"model": "claude-sonnet-5-5", "max_tokens": 900, "system": "You read invoices.",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "read this"},
                                                 {"type": "image", "source": {"type": "base64",
                                                                              "media_type": "image/png", "data": "AAA"}}]},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "record_answer",
                                                       "input": {"total": 5}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                                  "content": "total: 5 is not of type 'string'"}]}],
                "tools": [TOOL], "tool_choice": {"type": "tool", "name": "record_answer"}}
        g = px.to_gemini(body, model="gemini-2.5-flash")
        self.assertEqual(g["systemInstruction"], {"parts": [{"text": "You read invoices."}]})
        self.assertEqual([c["role"] for c in g["contents"]], ["user", "model", "user"])
        self.assertEqual(g["contents"][0]["parts"][1], {"inlineData": {"mimeType": "image/png", "data": "AAA"}})
        self.assertEqual(g["contents"][1]["parts"][0], {"functionCall": {"name": "record_answer",
                                                                         "args": {"total": 5}}})
        self.assertEqual(g["contents"][2]["parts"][0], {"functionResponse": {
            "name": "record_answer", "response": {"error": "total: 5 is not of type 'string'"}}})
        self.assertEqual(g["toolConfig"], {"functionCallingConfig": {"mode": "ANY",
                                                                     "allowedFunctionNames": ["record_answer"]}})
        self.assertEqual(g["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"], TOOL["input_schema"])
        self.assertEqual(g["generationConfig"], {"temperature": 0, "maxOutputTokens": 900,
                                                 "thinkingConfig": {"thinkingBudget": 0}})

    def test_older_schema_form(self):
        s = px.to_openapi(TOOL["input_schema"])
        p = s["properties"]
        self.assertEqual((s["type"], p["kind"], p["note"]),
                         ("OBJECT", {"type": "STRING", "enum": ["a", "b"]}, {"type": "STRING", "nullable": True}))
        self.assertEqual(p["extra"]["type"], "OBJECT")
        self.assertTrue(p["extra"]["properties"])                 # Gemini refuses an object with no properties
        self.assertEqual(p["rows"], {"type": "ARRAY", "items": {"type": "INTEGER"}})

    def test_response_and_errors(self):
        msg = px.to_anthropic(gemini_reply({"total": "5.00", "kind": "a"}), "gemini-2.5-flash")
        self.assertEqual((msg["stop_reason"], msg["content"][0]["input"], msg["usage"]),
                         ("tool_use", {"total": "5.00", "kind": "a"}, {"input_tokens": 120, "output_tokens": 35}))
        self.assertEqual(px.to_anthropic(gemini_reply(finish="MAX_TOKENS"), "m")["stop_reason"], "max_tokens")
        with self.assertRaises(px.UpstreamError):
            px.to_anthropic({"promptFeedback": {"blockReason": "SAFETY"}}, "m")
        self.assertEqual(px.anthropic_status(400, "API key not valid. Please pass a valid API key."), 401)
        self.assertEqual(px.anthropic_status(503, "overloaded"), 529)
        self.assertEqual(px.anthropic_status(404, "models/x is not found"), 404)

    def test_models(self):
        g = FakeGemini([])
        self.assertEqual((g.model_for("claude-opus-5-5"), g.model_for("claude-haiku-5-5")),
                         ("gemini-2.5-pro", "gemini-2.5-flash"))


    def test_agent_turns(self):
        """The case reviewer's loop: several tools, tool_choice any, a call and its result replayed as function
        parts, and Gemini's thought signature handed back with the call it came with."""
        reply = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "search_documents",
                                                                         "args": {"text": "TE97"}},
                                                        "thoughtSignature": "sig-1"}]}, "finishReason": "STOP"}]}
        msg = px.to_anthropic(reply, "gemini-2.5-flash")
        call = msg["content"][0]
        body = {"model": "claude-opus-5-5", "max_tokens": 900, "system": "Review.", "tool_choice": {"type": "any"},
                "tools": [TOOL, {**TOOL, "name": "search_documents"}],
                "messages": [{"role": "user", "content": [{"type": "text", "text": "case file"}]},
                             {"role": "assistant", "content": [call]},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                                           "content": "2 hits"}]}]}
        g = px.to_gemini(body, model="gemini-2.5-flash")
        self.assertEqual(g["toolConfig"], {"functionCallingConfig": {"mode": "ANY"}})
        self.assertEqual(len(g["tools"][0]["functionDeclarations"]), 2)
        self.assertEqual(g["contents"][1]["parts"][0], {"functionCall": {"name": "search_documents",
                                                                         "args": {"text": "TE97"}},
                                                        "thoughtSignature": "sig-1"})
        self.assertEqual(g["contents"][2]["parts"][0], {"functionResponse": {"name": "search_documents",
                                                                             "response": {"result": "2 hits"}}})


class ThroughTheSdk(unittest.TestCase):
    """The real Anthropic SDK and our gateway, talking to the proxy over HTTP."""

    def start(self, replies):
        backend = FakeGemini(replies)
        server = px.serve(backend, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        env = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
               "ANTHROPIC_API_KEY": "local-proxy"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        return backend, Gateway(max_retries=0)

    def ask(self, gw, models=("claude-sonnet-5-5",)):
        return gw.call_tool(purpose="t", version="1", models=list(models), system="You read invoices.",
                            content=[{"type": "text", "text": "read this"}], tool=TOOL, max_tokens=500)

    def test_tool_answer_round_trip(self):
        backend, gw = self.start([gemini_reply({"total": "5.00", "kind": "a"})])
        res = self.ask(gw)
        self.assertEqual((res.data, res.model), ({"total": "5.00", "kind": "a"}, "gemini-2.5-flash"))
        self.assertEqual(backend.sent[0][0], "gemini-2.5-flash")

    def test_repair_round_goes_through(self):
        backend, gw = self.start([gemini_reply({"total": 5, "kind": "a"}),
                                  gemini_reply({"total": "5.00", "kind": "a"})])
        self.assertEqual(self.ask(gw).data["total"], "5.00")
        self.assertEqual([c["role"] for c in backend.sent[1][1]["contents"]], ["user", "model", "user"])

    def test_agent_conversation_through_the_sdk(self):
        """Gateway.step, as the case reviewer drives it: a tool call, its result sent back, then the answer."""
        def call(name, args):
            return {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": name,
                                                                                              "args": args}}]},
                                    "finishReason": "STOP"}]}
        backend, gw = self.start([call("search_documents", {"text": "TE97"}), call("record_answer", {"total": "1"})])
        tools = [TOOL, {**TOOL, "name": "search_documents"}]
        msgs = [{"role": "user", "content": [{"type": "text", "text": "case file"}]}]
        first = gw.step(purpose="t", version="1", models=["claude-opus-5-5"], system="Review.", messages=msgs,
                        tools=tools, max_tokens=500)
        self.assertEqual([c["name"] for c in first.calls], ["search_documents"])
        msgs += [{"role": "assistant", "content": first.content},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": first.calls[0]["id"],
                                               "content": "2 hits"}]}]
        second = gw.step(purpose="t", version="1", models=["claude-opus-5-5"], system="Review.", messages=msgs,
                         tools=tools, max_tokens=500)
        self.assertEqual(second.calls[0]["input"], {"total": "1"})
        sent = backend.sent[1][1]
        self.assertEqual([c["role"] for c in sent["contents"]], ["user", "model", "user"])
        self.assertEqual(sent["contents"][2]["parts"][0]["functionResponse"]["name"], "search_documents")
        self.assertEqual(sent["toolConfig"], {"functionCallingConfig": {"mode": "ANY"}})

    def test_missing_signature_falls_back_to_text_turns(self):
        backend, gw = self.start([px.UpstreamError(400, "Function call is missing a thought_signature in "
                                                        "functionCall parts."),
                                  gemini_reply({"total": "3.00", "kind": "a"})])
        msgs = [{"role": "user", "content": [{"type": "text", "text": "read this"}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_gone", "name": "record_answer",
                                                   "input": {"total": 3}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_gone", "is_error": True,
                                              "content": "total: 3 is not of type 'string'"}]}]
        res = gw.step(purpose="t", version="1", models=["claude-sonnet-5-5"], system="s", messages=msgs,
                      tools=[TOOL], max_tokens=500)
        self.assertEqual(res.calls[0]["input"]["total"], "3.00")
        self.assertIn("text", backend.sent[1][1]["contents"][1]["parts"][0])

    def test_bad_key_means_no_api(self):
        _, gw = self.start([px.UpstreamError(400, "API key not valid. Please pass a valid API key.")])
        with self.assertRaises(LLMUnavailable):
            self.ask(gw)

    def test_unknown_model_falls_back_to_the_next(self):
        backend, gw = self.start([px.UpstreamError(404, "models/gemini-2.5-pro is not found"),
                                  gemini_reply({"total": "1.00", "kind": "b"})])
        res = self.ask(gw, models=("claude-opus-5-5", "claude-sonnet-5-5"))
        self.assertEqual(([m for m, _ in backend.sent], res.data["kind"]), (["gemini-2.5-pro", "gemini-2.5-flash"], "b"))

    def test_schema_form_falls_back_once(self):
        backend, gw = self.start([px.UpstreamError(400, 'Invalid JSON payload received. Unknown name '
                                                        '"parametersJsonSchema"'),
                                  gemini_reply({"total": "2.00", "kind": "a"})])
        self.assertEqual(self.ask(gw).data["total"], "2.00")
        self.assertIn("parameters", backend.sent[1][1]["tools"][0]["functionDeclarations"][0])
        self.assertTrue(backend.legacy_schema)


if __name__ == "__main__":
    unittest.main()
