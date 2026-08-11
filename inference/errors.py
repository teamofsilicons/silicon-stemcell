"""Failures every provider produces, spelled the same way.

A provider can fail by running out of time, by running out of quota, or by not
being logged in. Each of those has to reach a Carbon as an ordinary reply, so
the fallback payloads are built here rather than in each provider.
"""
from __future__ import annotations

import json

from interface.progress import (
    provider_not_authenticated_message,
    redact_diagnostic_text,
)

TIMEOUT_MSG = (
    "SYSTEM: The manager provider stopped responding before it produced a "
    "complete tool result. Delegate long-running work to a worker and finish "
    "this turn promptly."
)

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "hit your limit",
    "too many requests",
    "quota exceeded",
    "overloaded",
)


class ProviderTimeoutError(TimeoutError):
    """A provider turn exceeded its absolute or inactivity deadline."""


def is_rate_limit(text) -> bool:
    """Whether some provider text is reporting an API rate limit."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def error_tools(value) -> str:
    """A Carbon-visible provider error that echoes no private material."""
    detail = redact_diagnostic_text(value, limit=300)
    if detail in {
        "[private manager tool invocation omitted]",
        "[advertising memory content omitted]",
    }:
        detail = "provider call failed"
    detail = detail or "provider call failed"
    return json.dumps({
        "tools": [
            {"tool": "reply", "message": f"Manager error: {detail}"},
            {"tool": "do_nothing"},
        ]
    })


def not_authenticated_tools(provider: str) -> str:
    """The reply that tells a Carbon their provider needs a login."""
    return json.dumps({
        "tools": [
            {
                "tool": "reply",
                "message": provider_not_authenticated_message(provider),
            },
            {"tool": "do_nothing"},
        ]
    })
