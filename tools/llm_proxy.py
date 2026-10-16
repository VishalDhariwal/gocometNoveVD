"""A local stand-in for the Anthropic API that answers with Google Gemini. For testing only.

The graders run the harness against Anthropic. To test the same harness without an Anthropic key, run this proxy and
point the harness at it with ANTHROPIC_BASE_URL. The harness code doesn't change at all: it still uses the Anthropic
SDK, the same prompts, tools and schemas. Only the model behind the API is different, so it's a faithful test of
everything except Claude's reading quality.

    export GEMINI_API_KEY=...                      # from Google AI Studio
    python tools/llm_proxy.py                      # terminal 1: listens on http://127.0.0.1:8765

    export ANTHROPIC_BASE_URL=http://127.0.0.1:8765
    export ANTHROPIC_API_KEY=local-proxy           # any value; the proxy never forwards it
    make grade                                     # terminal 2: or ingest / report by hand

The grader passes only PATH, ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL to the harness, which is why the switch is the
base URL and not a setting inside the harness. Use a fresh --state-dir for proxy runs: answers are cached per state
folder, and the cache key includes the base URL, so Gemini answers are never served to an Anthropic run anyway.

What it translates (Anthropic Messages API -> Gemini generateContent and back):
- system prompt, text, base64 images and PDFs;
- one forced tool (tool_choice) -> function calling in mode ANY, restricted to that function; tool_choice "any"
  (the case reviewer, several tools) -> mode ANY over all of them;
- tool schema -> parametersJsonSchema, or the older OpenAPI-style `parameters` if the model refuses that;
- tool turns (tool_use, then tool_result) -> functionCall / functionResponse parts, so the reviewer's agent loop and
  the readers' repair round both run as real function-calling conversations;
- usage, stop reasons, and errors with the status codes the SDK expects (401 bad key, 404 unknown model, 429 rate
  limit, 529 overloaded), so the harness's model fallback and retries behave as they would against Anthropic.
Model names: claude-opus-* -> --strong, everything else -> --fast.

Only the Python standard library is used; nothing new needs installing.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("llm_proxy")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class UpstreamError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


# ---- Anthropic request -> Gemini request -------------------------------------------------------------------------
# Gemini may attach a thought signature to a function call and wants it back when the call is replayed. The harness
# only knows the call by the tool_use id we gave it, so the proxy remembers id -> signature for the calls it served.
_SIGNATURES: dict[str, str] = {}
_SIG_LOCK = threading.Lock()


def _parts(content, names: dict | None = None, text_tools: bool = False) -> list[dict]:
    names = {} if names is None else names
    if isinstance(content, str):
        return [{"text": content}] if content else []
    out = []
    for b in content or []:
        kind = b.get("type")
        if kind == "text" and b.get("text"):
            out.append({"text": b["text"]})
        elif kind in ("image", "document") and (b.get("source") or {}).get("type") == "base64":
            src = b["source"]
            out.append({"inlineData": {"mimeType": src.get("media_type", "application/octet-stream"),
                                       "data": src["data"]}})
        elif kind == "document" and (b.get("source") or {}).get("type") == "text":
            out.append({"text": b["source"].get("data", "")})
        elif kind == "tool_use" and text_tools:   # fallback: the call and its result as plain text turns
            out.append({"text": f"(Your previous call to {b.get('name')} had these arguments: "
                                f"{json.dumps(b.get('input'), ensure_ascii=False)})"})
        elif kind == "tool_result" and text_tools:
            text = b.get("content")
            if isinstance(text, list):
                text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
            out.append({"text": f"{'Error: ' if b.get('is_error') else ''}{text}"})
        elif kind == "tool_use":   # the model's earlier call, replayed
            names[b.get("id")] = b.get("name")
            part = {"functionCall": {"name": b.get("name"), "args": b.get("input") or {}}}
            with _SIG_LOCK:
                sig = _SIGNATURES.get(b.get("id"))
            if sig:
                part["thoughtSignature"] = sig
            out.append(part)
        elif kind == "tool_result":
            text = b.get("content")
            if isinstance(text, list):
                text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
            key = "error" if b.get("is_error") else "result"
            out.append({"functionResponse": {"name": names.get(b.get("tool_use_id")) or "tool",
                                             "response": {key: text or ""}}})
    return out


def _system(system) -> str:
    if isinstance(system, list):
        return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return system or ""


_OPENAPI_KEEP = {"description", "enum", "nullable", "required"}


def to_openapi(schema):
    """JSON Schema -> the OpenAPI subset Gemini's `parameters` field takes (upper-case types, no unions, no
    open-ended objects)."""
    if not isinstance(schema, dict):
        return {"type": "STRING"}
    t = schema.get("type")
    out = {k: schema[k] for k in _OPENAPI_KEEP if k in schema}
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        if "null" in t:
            out["nullable"] = True
        t = non_null[0] if non_null else "string"
    if t is None and "enum" in schema:
        t = "string"
    if "enum" in out:
        out["enum"] = [str(x) for x in out["enum"] if x is not None]
    t = (t or "string").lower()
    if t == "object":
        props = {k: to_openapi(v) for k, v in (schema.get("properties") or {}).items()}
        if not props:
            props = {"text": {"type": "STRING", "description": "the content, written out as short fields"}}
        out["properties"] = props
        out["required"] = [r for r in schema.get("required") or [] if r in props]
        if not out["required"]:
            out.pop("required")
    elif t == "array":
        out["items"] = to_openapi(schema.get("items") or {"type": "string"})
    out["type"] = t.upper()
    return out


def _json_schema(schema: dict) -> dict:
    return {k: v for k, v in schema.items() if k != "$schema"}


def to_gemini(body: dict, *, legacy_schema: bool = False, thinking: int = 0, model: str = "",
              text_tools: bool = False) -> dict:
    contents, names = [], {}
    for m in body.get("messages") or []:
        parts = _parts(m.get("content"), names, text_tools)
        if not parts:
            continue
        role = "model" if m.get("role") == "assistant" else "user"
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"] += parts
        else:
            contents.append({"role": role, "parts": parts})
    out: dict = {"contents": contents}
    system = _system(body.get("system"))
    if system:
        out["systemInstruction"] = {"parts": [{"text": system}]}
    tools = body.get("tools") or []
    if tools:
        decls = []
        for t in tools:
            d = {"name": t["name"], "description": t.get("description", "")}
            if legacy_schema:
                d["parameters"] = to_openapi(t.get("input_schema") or {})
            else:
                d["parametersJsonSchema"] = _json_schema(t.get("input_schema") or {"type": "object"})
            decls.append(d)
        out["tools"] = [{"functionDeclarations": decls}]
        choice = body.get("tool_choice") or {}
        mode = {"tool": "ANY", "any": "ANY", "none": "NONE"}.get(choice.get("type"), "AUTO")
        cfg: dict = {"mode": mode}
        if choice.get("type") == "tool":
            cfg["allowedFunctionNames"] = [choice["name"]]
        out["toolConfig"] = {"functionCallingConfig": cfg}
    max_tokens = int(body.get("max_tokens") or 4096)
    gen: dict = {"temperature": 0, "maxOutputTokens": max_tokens}
    if "2.5" in model and "pro" in model:   # 2.5 Pro always thinks: give it room on top of the answer
        budget = max(128, thinking or 1024)
        gen["thinkingConfig"] = {"thinkingBudget": budget}
        gen["maxOutputTokens"] = max_tokens + budget
    elif "2.5" in model:                     # 2.5 Flash / Flash-Lite: no thinking unless asked for
        gen["thinkingConfig"] = {"thinkingBudget": thinking}
        gen["maxOutputTokens"] = max_tokens + thinking
    out["generationConfig"] = gen
    return out


# ---- Gemini response -> Anthropic message ------------------------------------------------------------------------
def to_anthropic(resp: dict, model: str) -> dict:
    feedback = resp.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise UpstreamError(400, f"Gemini blocked the request: {feedback['blockReason']}")
    cand = (resp.get("candidates") or [{}])[0]
    blocks = []
    for p in (cand.get("content") or {}).get("parts") or []:
        if p.get("thought"):
            continue
        if "functionCall" in p:
            fc = p["functionCall"]
            call_id = f"toolu_{random.getrandbits(64):016x}"
            if p.get("thoughtSignature"):
                with _SIG_LOCK:
                    if len(_SIGNATURES) > 20000:
                        _SIGNATURES.clear()
                    _SIGNATURES[call_id] = p["thoughtSignature"]
            blocks.append({"type": "tool_use", "id": call_id, "name": fc.get("name"), "input": fc.get("args") or {}})
        elif p.get("text", "").strip():
            blocks.append({"type": "text", "text": p["text"]})
    finish = cand.get("finishReason")
    if any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    elif finish == "MAX_TOKENS":
        stop = "max_tokens"
    else:
        stop = "end_turn"
    if not blocks:
        blocks = [{"type": "text", "text": f"(no answer; Gemini finishReason={finish})"}]
    usage = resp.get("usageMetadata") or {}
    return {"id": f"msg_{random.getrandbits(64):016x}", "type": "message", "role": "assistant", "model": model,
            "content": blocks, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": int(usage.get("promptTokenCount") or 0),
                      "output_tokens": int((usage.get("candidatesTokenCount") or 0) +
                                           (usage.get("thoughtsTokenCount") or 0))}}


ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
               404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error", 500: "api_error",
               529: "overloaded_error"}


def anthropic_status(status: int, message: str) -> int:
    """Gemini's status for a problem -> the status the Anthropic SDK reads the same way."""
    if "api key not valid" in message.lower() or "api_key_invalid" in message.lower():
        return 401
    if status == 404 or (status == 400 and "model" in message.lower() and "not found" in message.lower()):
        return 404
    if status == 503:
        return 529
    return status if status in ERROR_TYPES else (500 if status >= 500 else 400)


# ---- calling Gemini ----------------------------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_minute: int):
        self.gap = 60.0 / per_minute if per_minute > 0 else 0.0
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        if not self.gap:
            return
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next)
            self.next = start + self.gap
        if start > now:
            time.sleep(start - now)


class Gemini:
    def __init__(self, key: str, *, fast: str, strong: str, thinking: int, rpm: int, retries: int, timeout: float):
        self.key, self.fast, self.strong, self.thinking = key, fast, strong, thinking
        self.limiter, self.retries, self.timeout = RateLimiter(rpm), retries, timeout
        self.legacy_schema = False

    def model_for(self, claude_model: str) -> str:
        return self.strong if "opus" in (claude_model or "") else self.fast

    def post(self, model: str, payload: dict) -> dict:
        """One generateContent call. Raises UpstreamError with Gemini's status and message."""
        req = urllib.request.Request(GEMINI_URL.format(model=model), data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self.key},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                err = json.loads(text).get("error") or {}
            except ValueError:
                err = {}
            message = err.get("message") or text[:500]
            retry = None
            for d in err.get("details") or []:
                if str(d.get("@type", "")).endswith("RetryInfo") and d.get("retryDelay"):
                    retry = float(re.sub(r"[^0-9.]", "", d["retryDelay"]) or 0) or None
            raise UpstreamError(e.code, message, retry) from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise UpstreamError(529, f"can't reach Gemini: {e}") from None

    def messages(self, body: dict) -> dict:
        model = self.model_for(body.get("model", ""))
        attempt, text_tools = 0, False
        while True:
            payload = to_gemini(body, legacy_schema=self.legacy_schema, thinking=self.thinking, model=model,
                                text_tools=text_tools)
            self.limiter.wait()
            try:
                return to_anthropic(self.post(model, payload), model)
            except UpstreamError as e:
                if e.status == 400 and not text_tools and re.search(r"thought_?signature", e.message, re.I):
                    # a replayed call whose signature this proxy no longer has (it was restarted): send the
                    # conversation's tool turns as plain text instead
                    log.warning("Gemini wants a thought signature we don't have; replaying tool turns as text")
                    text_tools = True
                    continue
                if e.status == 400 and not self.legacy_schema and re.search(
                        r"parametersJsonSchema|parameters_json_schema|Unknown name|schema", e.message, re.I):
                    log.warning("Gemini refused parametersJsonSchema (%s); using the OpenAPI-style schema", e.message)
                    self.legacy_schema = True
                    continue
                if e.status in (429, 500, 503, 529) and attempt < self.retries:
                    attempt += 1
                    delay = e.retry_after or min(60.0, 2.0 * 2 ** attempt) + random.random()
                    log.info("Gemini %s, retrying in %.0f s: %s", e.status, delay, e.message[:120])
                    time.sleep(delay)
                    continue
                raise


# ---- the HTTP server ---------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    backend = None          # set by serve(): something with .messages(body) -> dict

    def log_message(self, fmt, *args):   # quiet: our own log lines say what matters
        pass

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"ok": True, "proxy": "anthropic-to-gemini"})

    def do_POST(self):
        if not self.path.split("?")[0].rstrip("/").endswith("/v1/messages"):
            self._send(404, {"type": "error", "error": {"type": "not_found_error",
                                                         "message": f"only /v1/messages is served, not {self.path}"}})
            return
        started = time.monotonic()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            msg = self.backend.messages(body)
            log.info("%s -> %s: %s, %d in / %d out, %.1f s", body.get("model"), msg["model"], msg["stop_reason"],
                     msg["usage"]["input_tokens"], msg["usage"]["output_tokens"], time.monotonic() - started)
            self._send(200, msg)
        except UpstreamError as e:
            status = anthropic_status(e.status, e.message)
            log.warning("%s: %s %s", status, ERROR_TYPES.get(status, "api_error"), e.message[:200])
            self._send(status, {"type": "error", "error": {"type": ERROR_TYPES.get(status, "api_error"),
                                                           "message": f"(via Gemini) {e.message}"}})
        except Exception as e:  # noqa: BLE001 - a proxy bug must come back as an API error, not a hang
            log.exception("proxy error")
            self._send(500, {"type": "error", "error": {"type": "api_error", "message": f"proxy error: {e}"}})


def serve(backend, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"backend": backend})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Serve the Anthropic Messages API locally, answered by Gemini.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--fast", default=os.environ.get("GEMINI_FAST_MODEL", "gemini-3.5-flash"),
                    help="model for claude-sonnet / claude-haiku requests (default gemini-3.5-flash)")
    ap.add_argument("--strong", default=os.environ.get("GEMINI_STRONG_MODEL", "gemini-3.5-flash"),
                    help="model for claude-opus requests: contracts and scans (default gemini-3.5-flash)")
    ap.add_argument("--thinking", type=int, default=0, help="thinking budget in tokens for 2.5 models (default 0)")
    ap.add_argument("--rpm", type=int, default=int(os.environ.get("GEMINI_RPM", "0")),
                    help="most requests per minute to send (0 = no limit; free-tier keys need about 8)")
    ap.add_argument("--retries", type=int, default=6, help="retries on 429 / 5xx before giving up")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args(argv)
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("Set GEMINI_API_KEY (or GOOGLE_API_KEY) first.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    backend = Gemini(key, fast=args.fast, strong=args.strong, thinking=args.thinking, rpm=args.rpm,
                     retries=args.retries, timeout=args.timeout)
    server = serve(backend, args.host, args.port)
    log.info("Anthropic API on http://%s:%d, answered by %s / %s", args.host, args.port, args.fast, args.strong)
    log.info("Now: export ANTHROPIC_BASE_URL=http://%s:%d ANTHROPIC_API_KEY=local-proxy", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
