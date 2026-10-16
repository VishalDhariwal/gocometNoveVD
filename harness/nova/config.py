"""Settings.

The grader passes only PATH, HOME, TMPDIR and ANTHROPIC_* to the harness, so every default here has to work with no
environment at all. The NOVA_* variables exist for local experiments only.

Logging rule for the whole package: diagnostics go to stderr, never stdout. `report` prints its JSON on stdout.
"""
from __future__ import annotations

import os


def _list(var: str, default: tuple[str, ...]) -> tuple[str, ...]:
    items = tuple(x.strip() for x in os.environ.get(var, "").split(",") if x.strip())
    return items or default


# Scanned pages are rare (about one per mailbox) and a misread digit becomes a wrong dispute, so they go to the
# strongest model first. The rest of the chain is the fallback for when a model isn't available on the key in use.
VISION_MODELS = _list("NOVA_VISION_MODELS", ("claude-opus-5-5", "claude-sonnet-5-5"))

# Readers (phase 2). Classification is a small, cheap call per PDF; bills, records and emails go to a mid-size model;
# contracts are few and everything rests on them, so they get the strongest model first.
CLASSIFY_MODELS = _list("NOVA_CLASSIFY_MODELS", ("claude-haiku-5-5", "claude-sonnet-5-5"))
READER_MODELS = _list("NOVA_READER_MODELS", ("claude-sonnet-5-5", "claude-opus-5-5"))
CONTRACT_MODELS = _list("NOVA_CONTRACT_MODELS", ("claude-opus-5-5", "claude-sonnet-5-5"))

# Reads run in parallel. The grader's practice limit is 600 s per step: stop starting new reads after the budget and
# let the next ingest pick up whatever is left.
READER_WORKERS = int(os.environ.get("NOVA_READER_WORKERS", "8"))
INGEST_BUDGET_S = float(os.environ.get("NOVA_INGEST_BUDGET", "420"))
# A document whose reads keep failing is left alone after this many tries (it stays visible in the run log).
MAX_READ_ATTEMPTS = 3

# Long edge, in pixels, of a scanned page rendered for Claude. The API scales larger images down anyway.
VISION_MAX_EDGE = int(os.environ.get("NOVA_VISION_MAX_EDGE", "1568"))

# Resolution for the local OCR cross-check. OCR reads small digits better at a higher resolution.
OCR_DPI = 200

# Per request. The SDK itself retries rate limits, overloads, 5xx and connection errors with backoff.
LLM_TIMEOUT_S = float(os.environ.get("NOVA_LLM_TIMEOUT", "180"))
LLM_MAX_RETRIES = 4

# The case reviewer (nova/review): a Claude agent that takes a second look at the cases code finds hard, during
# `report`. It runs inside the report step's 600 s, so it has its own time budget and a cap on cases; whatever doesn't
# fit keeps the verdict code gave it.
REVIEW_MODELS = _list("NOVA_REVIEW_MODELS", ("claude-opus-5-5", "claude-sonnet-5-5"))
REVIEW_MAX_CASES = int(os.environ.get("NOVA_REVIEW_CASES", "20"))
REVIEW_BUDGET_S = float(os.environ.get("NOVA_REVIEW_BUDGET", "300"))
REVIEW_WORKERS = int(os.environ.get("NOVA_REVIEW_WORKERS", "6"))
REVIEW_MAX_TURNS = 8


def review_enabled() -> bool:
    """Read at call time, so tests can switch the reviewer off (NOVA_REVIEW=off) without reloading this module."""
    return os.environ.get("NOVA_REVIEW", "on").strip().lower() not in ("0", "off", "false", "no")
