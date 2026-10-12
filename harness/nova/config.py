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

# Long edge, in pixels, of a scanned page rendered for Claude. The API scales larger images down anyway.
VISION_MAX_EDGE = int(os.environ.get("NOVA_VISION_MAX_EDGE", "1568"))

# Resolution for the local OCR cross-check. OCR reads small digits better at a higher resolution.
OCR_DPI = 200

# Per request. The SDK itself retries rate limits, overloads, 5xx and connection errors with backoff.
LLM_TIMEOUT_S = float(os.environ.get("NOVA_LLM_TIMEOUT", "180"))
LLM_MAX_RETRIES = 4
