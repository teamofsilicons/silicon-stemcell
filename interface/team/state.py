"""The durable record of what has been synced, and when.

Read defensively — an older Stemcell wrote this file — and written atomically.
A malformed schedule is repaired rather than raising, because a broken
timestamp must not stop a Silicon from syncing.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import paths as paths_module
import json
import math
import time
from pathlib import Path
from typing import Any


def _default_state() -> dict[str, Any]:
    return {
        "version": constants._STATE_VERSION,
        "identity": {},
        "context": {},
        "peers": {},
        "managed_peer_ids": [],
        "own": {},
        "draft_archives": [],
        "schedule": {},
    }


def _load_state(root: Path) -> dict[str, Any]:
    path = paths_module._state_file(root)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _default_state()
    if not isinstance(body, dict) or body.get("version") != constants._STATE_VERSION:
        return _default_state()
    state = _default_state()
    state.update(body)
    for key, fallback in (
        ("identity", {}),
        ("context", {}),
        ("peers", {}),
        ("own", {}),
        ("schedule", {}),
    ):
        if not isinstance(state.get(key), dict):
            state[key] = fallback
    if not isinstance(state.get("managed_peer_ids"), list):
        state["managed_peer_ids"] = []
    if not isinstance(state.get("draft_archives"), list):
        state["draft_archives"] = []
    schedule = state["schedule"]
    now = time.time()
    for key in ("last_reconcile_at", "last_attempt_at"):
        schedule[key] = _safe_schedule_timestamp(
            schedule.get(key),
            now=now,
            allow_future=False,
        )
    schedule["next_reconcile_at"] = _safe_schedule_timestamp(
        schedule.get("next_reconcile_at"),
        now=now,
        allow_future=True,
    )
    failure_count = schedule.get("failure_count")
    if (
        isinstance(failure_count, bool)
        or not isinstance(failure_count, int)
        or failure_count < 0
    ):
        failure_count = 0
    schedule["failure_count"] = min(failure_count, 100)
    return state


def _safe_schedule_timestamp(
    value: Any,
    *,
    now: float,
    allow_future: bool,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(parsed) or parsed < 0:
        return 0.0
    if not allow_future and parsed > now + constants.RECONCILE_INTERVAL_SECONDS:
        return 0.0
    if allow_future and parsed > now + (2 * constants.RECONCILE_INTERVAL_SECONDS):
        return 0.0
    return parsed


def _save_state(root: Path, state: dict[str, Any]) -> None:
    state["version"] = constants._STATE_VERSION
    encoded = (
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    path = paths_module._state_file(root)
    try:
        if path.read_bytes() == encoded:
            return
    except OSError:
        pass
    paths_module._atomic_write_bytes(root, path, encoded)


def _record_reconcile_success(
    state: dict[str, Any],
    *,
    now: float,
    partial: bool,
) -> None:
    state["schedule"] = {
        "last_reconcile_at": now,
        "last_attempt_at": now,
        "next_reconcile_at": now + (10 if partial else constants.RECONCILE_INTERVAL_SECONDS),
        "failure_count": 0,
    }


def _record_reconcile_failure(state: dict[str, Any], *, now: float) -> None:
    schedule = state.setdefault("schedule", {})
    raw_failures = schedule.get("failure_count")
    failures = (
        raw_failures
        if isinstance(raw_failures, int)
        and not isinstance(raw_failures, bool)
        and raw_failures >= 0
        else 0
    ) + 1
    delay = min(constants.RECONCILE_INTERVAL_SECONDS, max(10, 2 ** min(failures, 6)))
    schedule.update(
        {
            "last_attempt_at": now,
            "next_reconcile_at": now + delay,
            "failure_count": failures,
        }
    )


def _managed_peer_ids(state: dict[str, Any]) -> set[str]:
    managed: set[str] = set()
    for raw in state.get("managed_peer_ids", []):
        try:
            managed.add(paths_module._validate_identifier(raw, "Silicon ID"))
        except errors_module.TeamContextError:
            continue
    return managed
