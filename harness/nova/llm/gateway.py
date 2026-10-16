"""One door to the Claude API for the whole harness.

Every call needs the same four things, so they live here once:

- a cache keyed by the full request, so a re-run costs nothing and gives exactly the same answer;
- model fallback, because we don't know which models the grader's key allows;
- forced structured output (one tool the model must call), validated against the tool's JSON schema, with one repair
  round when it doesn't match;
- errors that tell the caller what happened instead of crashing it: LLMUnavailable (no API access at all) or
  LLMFailed (this one request didn't produce a valid answer).

`call_tool` is one forced answer (the readers). `step` is one turn of a tool-using conversation (the case reviewer,
nova/review), cached by the conversation so far. The gateway itself decides nothing: every answer comes back in a
fixed shape that code checks.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import anthropic
import jsonschema

from .. import config

MAX_OUTPUT_TOKENS = 16000


class LLMUnavailable(RuntimeError):
    """No usable API access: no credentials, credentials rejected, or no model in the chain is available."""


class LLMFailed(RuntimeError):
    """The API answered, but not with a valid result for this request."""


class _ModelRefused(Exception):
    """This model isn't available to the key in use; the next model in the chain may be."""


@dataclass
class ToolResult:
    data: dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False


@dataclass
class StepResult:
    content: list[dict]       # the model's blocks as plain dicts: {"type": "text", ...} / {"type": "tool_use", ...}
    stop_reason: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False

    @property
    def calls(self) -> list[dict]:
        return [b for b in self.content if b.get("type") == "tool_use"]


def _plain_block(b: Any) -> dict | None:
    kind = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
    get = (lambda k: b.get(k)) if isinstance(b, dict) else (lambda k: getattr(b, k, None))
    if kind == "text":
        return {"type": "text", "text": get("text") or ""}
    if kind == "tool_use":
        data = get("input")
        return {"type": "tool_use", "id": get("id"), "name": get("name"), "input": data if isinstance(data, dict) else {}}
    return None   # thinking and anything else isn't replayed


class Cache(Protocol):
    def get(self, key: str) -> dict | None: ...

    def put(self, key: str, value: dict) -> None: ...


def fingerprint(request: Any) -> str:
    """sha256 of a request in canonical JSON. Base64 image data is replaced by its own hash first, so keys stay
    cheap to compute and nothing large is hashed twice."""

    def slim(x: Any) -> Any:
        if isinstance(x, dict):
            if x.get("type") == "base64" and isinstance(x.get("data"), str):
                return {**x, "data": hashlib.sha256(x["data"].encode()).hexdigest()}
            return {k: slim(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [slim(v) for v in x]
        return x

    blob = json.dumps(slim(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _schema_errors(data: Any, tool: dict) -> list[str]:
    if not isinstance(data, dict):
        return ["the answer is not a JSON object"]
    validator = jsonschema.Draft202012Validator(tool["input_schema"])
    return [f"{'/'.join(map(str, e.path)) or '(top level)'}: {e.message}" for e in validator.iter_errors(data)]


def _model_refused(e: Exception) -> bool:
    if isinstance(e, (anthropic.NotFoundError, anthropic.PermissionDeniedError)):
        return True
    return isinstance(e, anthropic.BadRequestError) and "model" in str(e).lower()


class Gateway:
    """Usage: Gateway(store.cache).call_tool(...), where store is a nova.store.Store (the cache is its llm_cache table).

    `client` is for tests: anything with .messages.create(**kwargs) that returns an object shaped like the SDK's
    Message. Without it, the real SDK client is created on first use, with credentials and base URL read from the
    environment (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL), as the grader provides them. The key is never logged.
    """

    def __init__(self, cache: Cache | None = None, client: Any = None, *, timeout: float | None = None,
                 max_retries: int | None = None):
        self.cache = cache
        self._client = client
        self._timeout = config.LLM_TIMEOUT_S if timeout is None else timeout
        self._max_retries = config.LLM_MAX_RETRIES if max_retries is None else max_retries
        self.api_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()  # readers call the gateway from several threads at once

    @property
    def client(self) -> Any:
        with self._lock:
            if self._client is None:
                if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                    raise LLMUnavailable("ANTHROPIC_API_KEY is not set in the environment")
                try:
                    self._client = anthropic.Anthropic(timeout=self._timeout, max_retries=self._max_retries)
                except Exception as e:  # noqa: BLE001 - any construction failure means no access
                    raise LLMUnavailable(f"can't create the Anthropic client ({type(e).__name__}): {e}") from e
            return self._client

    def _count(self, n_in: int, n_out: int) -> None:
        with self._lock:
            self.api_calls += 1
            self.input_tokens += n_in
            self.output_tokens += n_out

    def call_tool(self, *, purpose: str, version: str, models: Sequence[str], system: str, content: list[dict],
                  tool: dict, max_tokens: int = 4096) -> ToolResult:
        """Send one user message, make the model answer by calling `tool`, and return the tool input, validated.

        `purpose` and `version` are part of the cache key: bump `version` whenever the prompt or schema changes, or
        the cache will keep serving answers to the old prompt.
        """
        if not models:
            raise LLMUnavailable("no models configured")
        # The endpoint is part of the key: answers from a stand-in (tools/llm_proxy.py) are never served to a real run.
        key = fingerprint({"purpose": purpose, "version": version, "models": list(models), "system": system,
                           "content": content, "tool": tool, "endpoint": os.environ.get("ANTHROPIC_BASE_URL") or ""})
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit and not _schema_errors(hit.get("data"), tool):
                return ToolResult(hit["data"], hit["model"], hit.get("input_tokens", 0), hit.get("output_tokens", 0),
                                  cached=True)
        refused: list[str] = []
        for model in models:
            try:
                result = self._ask(model, system, content, tool, max_tokens)
            except _ModelRefused as e:
                refused.append(f"{model}: {e}")
                continue
            if self.cache is not None:
                self.cache.put(key, {"data": result.data, "model": result.model, "input_tokens": result.input_tokens,
                                     "output_tokens": result.output_tokens, "purpose": purpose, "version": version})
            return result
        raise LLMUnavailable("no model in the chain is available: " + "; ".join(refused))

    def _send(self, model: str, **kw: Any) -> Any:
        try:
            return self.client.messages.create(model=model, **kw)
        except anthropic.AuthenticationError as e:
            raise LLMUnavailable(f"credentials rejected: {e}") from e
        except LLMUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 - classified below; the SDK already retried transient errors
            if _model_refused(e):
                raise _ModelRefused(str(e)) from e
            raise LLMFailed(f"{model}: {type(e).__name__}: {e}") from e

    def step(self, *, purpose: str, version: str, models: Sequence[str], system: str, messages: list[dict],
             tools: list[dict], max_tokens: int = 4096) -> StepResult:
        """One turn of a conversation in which the model must call at least one of `tools`. An agent loop calls this
        again with the tool results added. Cached by the whole conversation so far, so a re-run on the same evidence
        replays exactly the same turns and reaches exactly the same answer."""
        if not models:
            raise LLMUnavailable("no models configured")
        key = fingerprint({"purpose": purpose, "version": version, "models": list(models), "system": system,
                           "messages": messages, "tools": tools,
                           "endpoint": os.environ.get("ANTHROPIC_BASE_URL") or ""})
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit and (hit.get("data") or {}).get("content"):
                d = hit["data"]
                return StepResult(d["content"], d.get("stop_reason") or "tool_use", hit["model"],
                                  hit.get("input_tokens", 0), hit.get("output_tokens", 0), cached=True)
        refused: list[str] = []
        for model in models:
            budget, used_in, used_out = max_tokens, 0, 0
            try:
                while True:   # once more with a bigger budget if the answer was cut off
                    resp = self._send(model, max_tokens=budget, system=system, messages=messages, tools=tools,
                                      tool_choice={"type": "any"})
                    usage = getattr(resp, "usage", None)
                    n_in, n_out = getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
                    used_in, used_out = used_in + n_in, used_out + n_out
                    self._count(n_in, n_out)
                    if resp.stop_reason != "max_tokens" or budget >= MAX_OUTPUT_TOKENS:
                        break
                    budget = min(budget * 2, MAX_OUTPUT_TOKENS)
            except _ModelRefused as e:
                refused.append(f"{model}: {e}")
                continue
            content = [b for b in (_plain_block(x) for x in resp.content) if b is not None]
            result = StepResult(content, resp.stop_reason, getattr(resp, "model", None) or model, used_in, used_out)
            if self.cache is not None and result.calls and resp.stop_reason != "max_tokens":
                self.cache.put(key, {"data": {"content": content, "stop_reason": resp.stop_reason},
                                     "model": result.model, "input_tokens": used_in, "output_tokens": used_out,
                                     "purpose": purpose, "version": version})
            return result
        raise LLMUnavailable("no model in the chain is available: " + "; ".join(refused))

    def _ask(self, model: str, system: str, content: list[dict], tool: dict, max_tokens: int) -> ToolResult:
        messages: list[dict] = [{"role": "user", "content": content}]
        budget = max_tokens
        used_in = used_out = 0
        problem = "no answer"
        for _ in range(3):  # the first try plus up to two fixes: an answer cut off, or one that breaks the schema
            resp = self._send(model, max_tokens=budget, system=system, messages=messages, tools=[tool],
                              tool_choice={"type": "tool", "name": tool["name"]})
            usage = getattr(resp, "usage", None)
            n_in, n_out = getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
            used_in, used_out = used_in + n_in, used_out + n_out
            self._count(n_in, n_out)

            if resp.stop_reason == "max_tokens":
                problem = f"the answer was cut off at max_tokens={budget}"
                if budget >= MAX_OUTPUT_TOKENS:
                    break
                budget = min(budget * 2, MAX_OUTPUT_TOKENS)
                continue
            call = next((b for b in resp.content
                         if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == tool["name"]), None)
            if call is None:
                problem = f"the model didn't call {tool['name']} (stop_reason={resp.stop_reason})"
                continue
            data = call.input if isinstance(call.input, dict) else None
            errors = _schema_errors(data, tool)
            if not errors:
                return ToolResult(data, getattr(resp, "model", None) or model, used_in, used_out)
            problem = f"the answer didn't match the schema: {errors[0]}"
            messages = messages[:1] + [
                {"role": "assistant",
                 "content": [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}]},
                {"role": "user",
                 "content": [{"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                              "content": f"{problem}. Call {tool['name']} again with input that matches its schema."}]},
            ]
        raise LLMFailed(f"{model}: {problem}")
