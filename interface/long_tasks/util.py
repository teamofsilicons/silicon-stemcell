"""Small readings a lifecycle needs: ids, fingerprints, goal estimates, liveness.
"""
from __future__ import annotations
from interface.long_tasks import constants
import hashlib
import math
import os
import re
import time
from typing import Any


def _is_internal_accuracy_review(context):
    """Return whether a queued root is a supersedable accuracy review."""
    text = str(context or "").lstrip()
    if text.startswith(constants._ACCURACY_REVIEW_MARKER):
        _, _, text = text.partition("\n")
        text = text.lstrip()
    return text.startswith(constants.ACCURACY_REVIEW_CONTEXT_PREFIX)


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _compact(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _terminal_reply_delivery_status(status: Any) -> bool:
    """Return true when replaying the same reply can never succeed."""
    text = str(status or "")
    if "idempotency_conflict" in text.lower():
        return True
    match = re.search(
        r"\b(?:HTTP|api)\s+([1-5][0-9]{2})\b",
        text,
        flags=re.IGNORECASE,
    )
    return bool(
        match
        and int(match.group(1))
        in {400, 404, 405, 409, 410, 413, 422}
    )


def _non_negative_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number <= 0:
        return 0.0
    return number


def _estimate_goal_from_data(data: Any) -> tuple[bool, float]:
    """Return estimate-field presence and its accepted displayed goal."""
    if not isinstance(data, dict):
        return False, 0.0
    timing = data.get("timing")
    sources = [data, timing] if isinstance(timing, dict) else [data]
    for source in sources:
        if "estimate_seconds" not in source:
            continue
        accepted = _non_negative_number(source.get("estimate_seconds"))
        return True, float(math.ceil(accepted)) if accepted else 0.0
    for source in sources:
        if "realistic_estimate_seconds" not in source:
            continue
        realistic = _non_negative_number(
            source.get("realistic_estimate_seconds")
        )
        if realistic:
            # This is the silicon-interface CLI's accepted transformation.
            return True, float(math.ceil(realistic * 1.05))
        return True, 0.0
    return False, 0.0


def _goal_materially_changed(previous: float, current: float) -> bool:
    previous = _non_negative_number(previous)
    current = _non_negative_number(current)
    if not previous or not current:
        return bool(previous) != bool(current)
    return abs(current - previous) >= max(1.0, previous * 0.01)


def _title_from_context(context: str) -> str:
    text = str(context or "")
    match = re.search(r"(?:^|\n)message:\s*(.*)", text, flags=re.S | re.I)
    body = match.group(1) if match else text
    ignored_prefixes = (
        "event_id:",
        "room_id:",
        "sender",
        "timestamp:",
        "attachment",
    )
    for raw_line in body.splitlines():
        line = " ".join(raw_line.split()).strip(" -")
        if not line or line.lower().startswith(ignored_prefixes):
            continue
        return line if len(line) <= 76 else line[:75].rstrip() + "…"
    return "Working on your request"


def _successful(result: Any) -> bool:
    return str(result or "").startswith("Done.")


def _retry_at(attempts: int) -> float:
    return time.time() + min(2 ** min(max(1, attempts), 6), constants.RETRY_MAX_SECONDS)


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _bounded_mapping(value: Any, limit: int = constants.MAX_ALIASES) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    items = list(value.items())[-limit:]
    return {
        _compact(key, 256): _compact(mapped, 256)
        for key, mapped in items
        if key and mapped
    }
