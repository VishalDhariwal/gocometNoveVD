"""A local stand-in for the Anthropic API, answered by OpenAI. For testing only.

The graders run the harness against Anthropic (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL). To test the same harness on an
OpenAI key, run this proxy and point the harness at it with ANTHROPIC_BASE_URL. The harness doesn't change at all: the
same Anthropic SDK, prompts, tools and schemas. Only the model behind the API differs, so it tests everything except
Claude's own reading.

    export OPENAI_API_KEY=...
    python tools/llm_proxy.py --check                     # the key, the model, and this key's rate limits
    python tools/llm_proxy.py --call-log calls.jsonl      # listens on http://127.0.0.1:8765

    export ANTHROPIC_BASE_URL=http://127.0.0.1:8765 ANTHROPIC_API_KEY=local-proxy
    python harness/harness.py ingest ...                  # tools/live_run.sh does all of this and times it

What it translates (Anthropic Messages API -> OpenAI chat completions and back):
- the system prompt, text and base64 images (the harness sends scanned pages as images);
- one forced tool (tool_choice) -> that function, forced; tool_choice "any" (the case reviewer) -> "required";
- tool turns (tool_use, then tool_result) -> assistant tool_calls and "tool" messages, so the reviewer's agent loop and
  the readers' repair round run as real function-calling conversations;
- usage (input, cached input, output), stop reasons, and errors with the status codes the SDK expects: 401 bad key,
  404 unknown model, 429 rate limit, 529 overloaded. No credit left, or a daily limit, comes back as "model not
  available", so the harness stops reading and leaves the rest for a later run on the same state folder.
Model names: claude-opus-* -> --strong, everything else -> --fast (both gpt-4o-mini unless set).

Rate limits: the key's per-minute request and token limits (read from OpenAI's response headers) are kept to. A
request goes out only when it fits what was sent in the last minute, and a 429 is waited out as OpenAI asks. OpenAI
counts a request's input plus its max_completion_tokens against the token limit, so that is what has to fit.

--call-log writes one JSON line per request (tokens, cached tokens, seconds on the API, seconds queued, retries);
tools/summarize_run.py turns it into the run's numbers. --cache-dir keeps answers for cheap re-runs; leave it off when
timing a run, because a cached answer takes no time.

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
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
from pathlib import Path

log = logging.getLogger("llm_proxy")
OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
OUTPUT_CAP = {"gpt-4o-mini": 16384, "gpt-4o": 16384, "gpt-4.1-mini": 32768, "gpt-4.1": 32768, "gpt-4.1-nano": 32768}
IMAGE_TOKENS = 1500        # what an image counts as when pacing, before OpenAI reports the real usage


class UpstreamError(Exception):
    def __init__(self, status: int, message: str, *, code: str | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code
        self.headers = headers or {}
        ms = _int(self.headers.get("retry-after-ms"))
        self.retry_after = (ms / 1000 if ms else None) or _duration(self.headers.get("retry-after")) or \
            _try_again_in(message)


def _int(x) -> int | None:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _duration(text) -> float | None:
    """'7.66s', '1m2.5s', '340ms', '2' -> seconds."""
    if text is None:
        return None
    s = str(text).strip()
    if re.fullmatch(r"[\d.]+", s):
        return float(s)
    parts = re.findall(r"([\d.]+)\s*(ms|h|m|s)", s)
    return sum(float(n) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[u] for n, u in parts) if parts else None


def _try_again_in(message: str) -> float | None:
    m = re.search(r"try again in ([\dhms.]+)", message or "")
    return _duration(m.group(1)) if m else None


def _no_capacity(e: UpstreamError) -> bool:
    """No credit left, or a daily limit: waiting a minute won't help."""
    return e.code == "insufficient_quota" or bool(re.search(r"per day|\(RPD\)|\(TPD\)", e.message or "", re.I))


# ---- Anthropic request -> OpenAI request ----------------------------------------------------------------------------
def _system(system) -> str:
    if isinstance(system, list):
        return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return system or ""


def _text_of(content) -> str:
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict))
    return content or ""


def _reasoning(model: str) -> bool:
    return bool(re.match(r"o\d|gpt-5", model))


def to_openai(body: dict, *, model: str, max_tokens: int) -> dict:
    msgs: list[dict] = []
    system = _system(body.get("system"))
    if system:
        msgs.append({"role": "system", "content": system})
    for m in body.get("messages") or []:
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        if m.get("role") == "assistant":
            msg: dict = {"role": "assistant",
                         "content": "\n".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text"))}
            calls = [{"id": b.get("id"), "type": "function",
                      "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                     for b in blocks if b.get("type") == "tool_use"]
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
            continue
        parts = []
        for b in blocks:
            kind, src = b.get("type"), b.get("source") or {}
            if kind == "tool_result":     # its own message, straight after the assistant turn that made the call
                msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                             "content": ("Error: " if b.get("is_error") else "") + _text_of(b.get("content"))})
            elif kind == "text" and b.get("text"):
                parts.append({"type": "text", "text": b["text"]})
            elif kind == "image" and src.get("type") == "base64":
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"}})
            elif kind == "document":
                parts.append({"type": "text", "text": src.get("data", "") if src.get("type") == "text" else
                              "(a PDF was attached here; this test proxy can't pass PDFs on)"})
        if parts:
            if all(p["type"] == "text" for p in parts):
                msgs.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
            else:
                msgs.append({"role": "user", "content": parts})
    out: dict = {"model": model, "messages": msgs, "max_completion_tokens": max_tokens}
    if not _reasoning(model):
        out["temperature"] = 0
    tools = body.get("tools") or []
    if tools:
        out["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": {k: v for k, v in (t.get("input_schema") or {"type": "object"}).items() if k != "$schema"}}}
            for t in tools]
        choice = body.get("tool_choice") or {}
        kind = choice.get("type")
        out["tool_choice"] = ({"type": "function", "function": {"name": choice["name"]}} if kind == "tool" else
                              "required" if kind == "any" else "none" if kind == "none" else "auto")
        if kind == "tool":
            out["parallel_tool_calls"] = False
    return out


def estimate_tokens(payload: dict) -> int:
    """A generous count of a request's input, for pacing only (OpenAI reports the real number afterwards)."""
    chars, images = len(json.dumps(payload.get("tools") or [])), 0
    for m in payload.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        for p in c if isinstance(c, list) else []:
            if p.get("type") == "image_url":
                images += 1
            else:
                chars += len(p.get("text") or "")
        chars += 16 + sum(len(tc["function"]["arguments"]) + 24 for tc in m.get("tool_calls") or [])
    return int(chars / 3.3) + images * IMAGE_TOKENS + 32


# ---- OpenAI response -> Anthropic message ---------------------------------------------------------------------------
def _json_args(text):
    """A tool call's arguments as a dict, or None. Tolerates code fences and text around the JSON."""
    if isinstance(text, dict):
        return text
    if text is None or (isinstance(text, str) and not text.strip()):
        return {}
    if not isinstance(text, str):
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    for cand in (s, s[s.find("{"): s.rfind("}") + 1] if "{" in s else ""):
        try:
            v = json.loads(cand)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return None


class BadToolCall(Exception):
    pass


def from_openai(resp: dict, model: str) -> dict:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish = choice.get("finish_reason")
    blocks = []
    text = (msg.get("content") or msg.get("refusal") or "").strip()
    if text:
        blocks.append({"type": "text", "text": text})
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        args = _json_args(fn.get("arguments"))
        if args is None:
            if finish == "length":         # cut off mid-call: say so, and the harness asks again with more room
                blocks = [{"type": "text", "text": "(the answer was cut off)"}]
                break
            raise BadToolCall(f"{fn.get('name')}: {str(fn.get('arguments'))[:300]}")
        blocks.append({"type": "tool_use", "id": c.get("id") or f"toolu_{random.getrandbits(64):016x}",
                       "name": fn.get("name"), "input": args})
    stop = ("tool_use" if any(b["type"] == "tool_use" for b in blocks) else
            "max_tokens" if finish == "length" else "end_turn")
    if not blocks:
        blocks = [{"type": "text", "text": f"(no answer; finish_reason={finish})"}]
    usage = resp.get("usage") or {}
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return {"id": f"msg_{random.getrandbits(64):016x}", "type": "message", "role": "assistant", "model": model,
            "content": blocks, "stop_reason": stop, "stop_sequence": None,
            # input_tokens is the whole input, cached part included, so the harness's own count is the total
            "usage": {"input_tokens": int(usage.get("prompt_tokens") or 0), "cache_read_input_tokens": cached,
                      "output_tokens": int(usage.get("completion_tokens") or 0)}}


ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
               404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error", 500: "api_error",
               529: "overloaded_error"}


def anthropic_error(e: UpstreamError) -> tuple[int, str]:
    """OpenAI's status and message -> what the Anthropic SDK (and our gateway) reads the same way."""
    if e.status == 404 or e.code == "model_not_found":
        return 404, e.message
    if e.status >= 500:
        return 529, e.message
    message = e.message
    if e.status == 400:
        # The gateway takes a 400 that mentions the model as "this model isn't available" and moves down its model
        # chain; an ordinary bad request mustn't read that way.
        message = re.sub(r"\bmodel\b", "LLM", message, flags=re.I)
    return (e.status if e.status in ERROR_TYPES else 400), message


# ---- pacing -----------------------------------------------------------------------------------------------------------
class Window:
    """Keeps one model under its per-minute limits. It counts what was sent in the last 60 s and follows the remaining
    counters OpenAI returns with every answer (assumed to refill evenly until their reset time). A request goes out
    only when it fits both."""

    def __init__(self, rpm: int, tpm: int, fixed: bool = False):
        self.rpm, self.tpm, self.fixed = rpm, tpm, fixed
        self.sent: collections.deque = collections.deque()     # [time, tokens], oldest first
        self.cv = threading.Condition()
        self.remaining: int | None = None
        self.seen_at = self.refill_s = 0.0
        self.blocked_until = 0.0

    def wait_needed(self, need: int, now: float) -> float:
        while self.sent and self.sent[0][0] <= now - 60:
            self.sent.popleft()
        need = min(need, self.tpm) if self.tpm else need
        wait = self.blocked_until - now
        if self.rpm and len(self.sent) >= self.rpm:
            wait = max(wait, self.sent[len(self.sent) - self.rpm][0] + 60 - now)
        if self.tpm and self.sent:
            used, freed = sum(t for _, t in self.sent), 0
            for t0, tok in self.sent:
                if used - freed + need <= self.tpm:
                    break
                freed += tok
                wait = max(wait, t0 + 60 - now)
        if self.remaining is not None and self.tpm and self.refill_s > 0:
            since = sum(t for t0, t in self.sent if t0 > self.seen_at)
            rate = (self.tpm - self.remaining) / self.refill_s      # tokens per second until it's full again
            short = need + since - self.remaining
            if short > 0 and rate > 0:
                wait = max(wait, self.seen_at + short / rate - now)
        return wait

    def acquire(self, need: int, reserve: int) -> list:
        with self.cv:
            while True:
                now = time.monotonic()
                wait = self.wait_needed(need, now)
                if wait <= 0:
                    entry = [now, reserve]
                    self.sent.append(entry)
                    return entry
                self.cv.wait(timeout=min(wait, 5.0) + 0.05)

    def settle(self, entry: list | None, tokens: int, headers: dict | None) -> None:
        with self.cv:
            if entry is not None:
                entry[1] = tokens
            h, now = headers or {}, time.monotonic()
            if not self.fixed:
                self.tpm = _int(h.get("x-ratelimit-limit-tokens")) or self.tpm
                self.rpm = _int(h.get("x-ratelimit-limit-requests")) or self.rpm
            left = _int(h.get("x-ratelimit-remaining-tokens"))
            if left is not None:
                self.remaining, self.seen_at = left, now
                self.refill_s = _duration(h.get("x-ratelimit-reset-tokens")) or 0.0
            if _int(h.get("x-ratelimit-remaining-requests")) == 0:
                self.blocked_until = max(self.blocked_until, now + (_duration(h.get("x-ratelimit-reset-requests")) or 1))
            after = _duration(h.get("retry-after"))
            if after:
                self.blocked_until = max(self.blocked_until, now + after)
            self.cv.notify_all()


# ---- calling OpenAI ----------------------------------------------------------------------------------------------------
class OpenAI:
    label = "OpenAI"

    def __init__(self, key: str, *, fast: str = DEFAULT_MODEL, strong: str | None = None, rpm: int = 0, tpm: int = 0,
                 retries: int = 6, timeout: float = 300.0, concurrency: int = 16, cache_dir: str | None = None,
                 call_log: str | None = None, url: str = OPENAI_URL):
        self.key, self.fast, self.strong, self.url = key, fast, strong or fast, url.rstrip("/")
        self.rpm, self.tpm, self.retries, self.timeout = rpm, tpm, retries, timeout
        self.gate = threading.BoundedSemaphore(max(1, concurrency))
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.call_log = Path(call_log) if call_log else None
        self.windows: dict[str, Window] = {}
        self.lock = threading.Lock()

    def model_for(self, claude_model: str) -> str:
        return self.strong if "opus" in (claude_model or "") else self.fast

    def window(self, model: str) -> Window:
        with self.lock:
            if model not in self.windows:   # Tier 1 limits for gpt-4o-mini until the first answer's headers say more
                self.windows[model] = Window(self.rpm or 500, self.tpm or 200_000, fixed=bool(self.rpm or self.tpm))
            return self.windows[model]

    def _request(self, path: str, payload: dict | None = None) -> tuple[dict, dict]:
        req = urllib.request.Request(f"{self.url}{path}", data=json.dumps(payload).encode() if payload else None,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
                                     method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read()), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                err = json.loads(text).get("error") or {}
            except ValueError:
                err = {}
            raise UpstreamError(e.code, err.get("message") or text[:500], code=err.get("code") or err.get("type"),
                                headers={k.lower(): v for k, v in (e.headers or {}).items()}) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise UpstreamError(529, f"can't reach OpenAI: {e}") from None

    def post(self, payload: dict) -> tuple[dict, dict]:
        return self._request("/chat/completions", payload)

    def _cache_path(self, model: str, body: dict) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(json.dumps({"model": model, "body": {k: v for k, v in body.items() if k != "max_tokens"}},
                                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.json"

    def _log(self, **row) -> None:
        if self.call_log is None:
            return
        row = {"t": round(time.time(), 3), **row}
        with self.lock:
            self.call_log.parent.mkdir(parents=True, exist_ok=True)
            with self.call_log.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def messages(self, body: dict) -> dict:
        model = self.model_for(body.get("model", ""))
        choice = body.get("tool_choice") or {}
        tool = choice.get("name") if choice.get("type") == "tool" else ("any" if body.get("tools") else None)
        cached = self._cache_path(model, body)
        if cached is not None and cached.is_file():
            msg = json.loads(cached.read_text())
            u = msg["usage"]
            self._log(model=model, tool=tool, status="cached", input=u["input_tokens"],
                      cached_input=u.get("cache_read_input_tokens", 0), output=u["output_tokens"], seconds=0,
                      api_seconds=0, queued=0)
            return msg
        win = self.window(model)
        payload = to_openai(body, model=model, max_tokens=min(int(body.get("max_tokens") or 4096),
                                                              OUTPUT_CAP.get(model, 16384)))
        est = estimate_tokens(payload)
        if win.tpm and est + payload["max_completion_tokens"] > win.tpm * 0.95:
            payload["max_completion_tokens"] = max(256, int(win.tpm * 0.95) - est)
            log.warning("a request of about %d tokens: answer capped at %d tokens to fit %d tokens per minute",
                        est, payload["max_completion_tokens"], win.tpm)
        started, queued, api_s, attempt, bad_calls = time.monotonic(), 0.0, 0.0, 0, 0
        while True:
            t0 = time.monotonic()
            with self.gate:
                entry = win.acquire(est + payload["max_completion_tokens"],
                                    est + min(payload["max_completion_tokens"], 2000))
                t_api = time.monotonic()
                queued += t_api - t0
                err = None
                try:
                    resp, headers = self.post(payload)
                    win.settle(entry, _int((resp.get("usage") or {}).get("total_tokens")) or est, headers)
                except UpstreamError as e:
                    win.settle(entry, 0 if e.status == 429 else est, e.headers)
                    err = e
                api_s += time.monotonic() - t_api
            row = dict(model=model, tool=tool, estimate=est, max_out=payload["max_completion_tokens"], retries=attempt,
                       bad_calls=bad_calls, queued=round(queued, 2), api_seconds=round(api_s, 2))
            if err is None:
                try:
                    msg = from_openai(resp, model)
                except BadToolCall as e:
                    if bad_calls < 2:
                        bad_calls += 1
                        log.info("the tool call's arguments weren't JSON; asking again (%d): %s", bad_calls, e)
                        continue
                    self._log(status="failed", error=f"tool call arguments not JSON: {e}",
                              seconds=round(time.monotonic() - started, 2), **row)
                    raise UpstreamError(400, "the tool call's arguments weren't JSON after 3 tries") from None
                if cached is not None and msg["stop_reason"] == "tool_use":
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cached.with_suffix(f".{threading.get_ident()}.tmp")
                    tmp.write_text(json.dumps(msg, ensure_ascii=False))
                    tmp.replace(cached)
                u = msg["usage"]
                self._log(status="ok", input=u["input_tokens"], cached_input=u["cache_read_input_tokens"],
                          output=u["output_tokens"], stop=msg["stop_reason"],
                          seconds=round(time.monotonic() - started, 2), **row)
                return msg
            e = err
            if e.status == 429 and _no_capacity(e):
                self._log(status="no capacity", error=e.message[:300], seconds=round(time.monotonic() - started, 2),
                          **row)
                log.error("OpenAI has no capacity left for this key (%s): %s", e.code, e.message[:200])
                raise UpstreamError(403, f"no OpenAI capacity left for this key; continue later on the same state "
                                         f"folder ({e.message[:200]})")
            if (e.status == 429 or e.status >= 500) and attempt < self.retries:
                attempt += 1
                delay = e.retry_after or min(60.0, 2.0 * 2 ** attempt) + random.random()
                log.info("OpenAI %s, retrying in %.1f s: %s", e.status, delay, e.message[:120])
                time.sleep(delay)
                queued += delay
                continue
            self._log(status="failed", error=f"{e.status} {e.message[:300]}",
                      seconds=round(time.monotonic() - started, 2), **row)
            raise e

    def check(self) -> int:
        """Before a run: the key works, the models are there, a forced tool call comes back, and this key's limits."""
        probe = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        for model in sorted({self.fast, self.strong}):
            try:
                self._request(f"/models/{model}")
                resp, h = self.post({"model": model, "max_completion_tokens": 20,
                                     "messages": [{"role": "user", "content": "Call record_ok with ok set to true."}],
                                     "tools": [{"type": "function", "function": {"name": "record_ok",
                                                                                 "description": "Say ok.",
                                                                                 "parameters": probe}}],
                                     "tool_choice": {"type": "function", "function": {"name": "record_ok"}}})
            except UpstreamError as e:
                print(f"{model}: {e.status} {e.code or ''} {e.message}", file=sys.stderr)
                return 1
            calls = (resp.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
            ok = bool(calls) and calls[0]["function"]["name"] == "record_ok"
            print(f"{model}: tool call {'works' if ok else 'did NOT come back'}; this key's limits: "
                  f"{h.get('x-ratelimit-limit-requests', '?')} requests/min, {h.get('x-ratelimit-limit-tokens', '?')} "
                  f"tokens/min", file=sys.stderr)
            if not ok:
                return 1
        return 0


# ---- the HTTP server ---------------------------------------------------------------------------------------------------
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
        self._send(200, {"ok": True, "proxy": "anthropic-to-openai"})

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
            status, message = anthropic_error(e)
            log.warning("%s: %s %s", status, ERROR_TYPES.get(status, "api_error"), message[:200])
            self._send(status, {"type": "error", "error": {"type": ERROR_TYPES.get(status, "api_error"),
                                                           "message": f"(via OpenAI) {message}"}})
        except Exception as e:  # noqa: BLE001 - a proxy bug must come back as an API error, not a hang
            log.exception("proxy error")
            self._send(500, {"type": "error", "error": {"type": "api_error", "message": f"proxy error: {e}"}})


class Server(ThreadingHTTPServer):
    request_queue_size = 128    # the harness opens many connections at once; the default backlog of 5 drops some
    daemon_threads = True


def serve(backend, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"backend": backend})
    return Server((host, port), handler)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Serve the Anthropic Messages API locally, answered by OpenAI.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--fast", default=os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
                    help=f"model for claude-sonnet / claude-haiku requests (default $OPENAI_MODEL or {DEFAULT_MODEL})")
    ap.add_argument("--strong", default=os.environ.get("OPENAI_STRONG_MODEL"),
                    help="model for claude-opus requests: contracts and scans (default: as --fast)")
    ap.add_argument("--rpm", type=int, default=0, help="requests per minute to stay under (default: the key's own)")
    ap.add_argument("--tpm", type=int, default=0, help="tokens per minute to stay under (default: the key's own)")
    ap.add_argument("--concurrency", type=int, default=16, help="requests in flight at once")
    ap.add_argument("--cache-dir", help="keep every answer here; a re-run sends only the requests that changed")
    ap.add_argument("--call-log", help="one JSON line per request: tokens, seconds, retries")
    ap.add_argument("--check", action="store_true", help="test the key and models, show this key's limits, exit")
    ap.add_argument("--retries", type=int, default=6, help="retries on 429 / 5xx before giving up")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("Set OPENAI_API_KEY first.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    backend = OpenAI(key, fast=args.fast, strong=args.strong, rpm=args.rpm, tpm=args.tpm, retries=args.retries,
                     timeout=args.timeout, concurrency=args.concurrency, cache_dir=args.cache_dir,
                     call_log=args.call_log, url=os.environ.get("OPENAI_BASE_URL", OPENAI_URL))
    if args.check:
        return backend.check()
    server = serve(backend, args.host, args.port)
    log.info("Anthropic API on http://%s:%d, answered by OpenAI (%s / %s)", args.host, args.port, backend.fast,
             backend.strong)
    log.info("Now: export ANTHROPIC_BASE_URL=http://%s:%d ANTHROPIC_API_KEY=local-proxy", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
