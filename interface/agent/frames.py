"""Keeping one outbound frame inside what the connection will carry.

A frame that exceeds the budget is trimmed rather than dropped: the keys that
identify it survive, and the payload that made it large is replaced. Losing a
status frame is worse than losing the detail inside it.
"""
from __future__ import annotations

import json
import threading

SEND_LOCK = threading.Lock()

MAX_OUTBOUND_FRAME_BYTES = 120_000

# Keys that identify or route a frame. Truncating these would corrupt the
# protocol, so they are never candidates for trimming.
_PROTECTED_FRAME_KEYS = frozenset(
    {"type", "id", "run_id", "command", "session_id", "action", "ts", "status"}
)


def _frame_size(payload: dict) -> int:
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def bound_frame(payload: dict, budget: int = MAX_OUTBOUND_FRAME_BYTES):
    """Return a frame at or under `budget`, or None if it cannot be shrunk.

    Trims the largest non-protected field first and leaves a marker in its
    place, so an over-long frame arrives truncated-but-useful instead of
    costing the connection.
    """

    if _frame_size(payload) <= budget:
        return payload

    trimmed = dict(payload)
    while _frame_size(trimmed) > budget:
        candidates = [
            (len(json.dumps(v, separators=(",", ":"), default=str)), k)
            for k, v in trimmed.items()
            if k not in _PROTECTED_FRAME_KEYS
        ]
        if not candidates:
            return None
        size, key = max(candidates)
        if size <= 64:
            # Nothing left worth trimming; the frame is irreducibly too big.
            return None
        value = trimmed[key]
        if isinstance(value, str):
            keep = max(0, len(value) - (size - budget) - 512)
            trimmed[key] = value[:keep] + f"…[truncated {len(value) - keep} chars]"
        elif isinstance(value, list):
            trimmed[key] = [{"truncated_items": len(value)}]
        else:
            trimmed[key] = "[truncated]"
    return trimmed


def send_json(ws, payload: dict) -> bool:
    """Send one frame. Returns False if it was too large to send at all."""
    bounded = bound_frame(payload)
    # The type is attacker/bug-controlled like any other field, so bound it too
    # rather than letting a malformed frame flood the log.
    kind = str(payload.get("type", "?"))[:64]
    if bounded is None:
        print(
            f"[glass-agent] dropped oversized {kind} frame "
            f"({_frame_size(payload)} bytes)",
            flush=True,
        )
        return False
    if bounded is not payload:
        print(
            f"[glass-agent] truncated oversized {kind} frame "
            f"({_frame_size(payload)} -> {_frame_size(bounded)} bytes)",
            flush=True,
        )
    with SEND_LOCK:
        ws.send(json.dumps(bounded, separators=(",", ":")))
    return True


