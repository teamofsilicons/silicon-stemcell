"""Ids, timestamps and result shapes. Pure, lock-free, safe inside a guard.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any


def _safe_fragment(value: Any, fallback: str = "item") -> str:
    cleaned = "".join(
        char if char.isalnum() or char in "._:-" else "-"
        for char in str(value or "").strip()
    ).strip("-._:")
    return (cleaned or fallback)[:48]


def _new_id(prefix: str, hint: Any = "") -> str:
    prefix = _safe_fragment(prefix, "work")
    hint = _safe_fragment(hint, "")
    token = uuid.uuid4().hex[:20]
    return f"{prefix}:{hint}:{token}"[:128] if hint else f"{prefix}:{token}"


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{_safe_fragment(prefix, 'work')}:{digest}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _compact(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _result_data(value: Any) -> dict[str, Any]:
    current = value
    for _ in range(3):
        if not isinstance(current, dict):
            return {}
        for key in ("data", "result"):
            nested = current.get(key)
            if isinstance(nested, dict):
                current = nested
                break
        else:
            return current
    return current if isinstance(current, dict) else {}


def _public_result(value: Any) -> str:
    data = _result_data(value)
    if data:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _compact(value, limit=2_000)
