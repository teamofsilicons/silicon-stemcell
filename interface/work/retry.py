"""One journal entry's lease, attempts, and how it ends.

The owner token is process-unique and re-derived after a fork, so two processes
can never both believe they hold the same lane.
"""
from __future__ import annotations

from interface.work import constants
from interface.work import payloads as payloads_module
from interface.work import store as store_module
import os
import random
import re
import threading
import time
import uuid
from copy import deepcopy
from typing import Any


_CALL_RETRY_LOCK = threading.Lock()


_CALL_RETRY_INFLIGHT: set[str] = set()


_CALL_RETRY_PROCESS_PID = os.getpid()


_CALL_RETRY_PROCESS_TOKEN = uuid.uuid4().hex


def _call_retry_owner() -> str:
    """Return a process-unique lease owner, including after a fork."""
    global _CALL_RETRY_PROCESS_PID, _CALL_RETRY_PROCESS_TOKEN
    pid = os.getpid()
    with _CALL_RETRY_LOCK:
        if pid != _CALL_RETRY_PROCESS_PID:
            _CALL_RETRY_PROCESS_PID = pid
            _CALL_RETRY_PROCESS_TOKEN = uuid.uuid4().hex
            _CALL_RETRY_INFLIGHT.clear()
        return f"{pid}:{_CALL_RETRY_PROCESS_TOKEN}"


def _claim_call_retry(
    retry_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Claim the oldest mutation in a call lane across rolling processes."""
    now = time.time() if now is None else float(now)
    owner = _call_retry_owner()
    with store_module._state_guard():
        state = store_module._read_state()
        # Persist retention pruning even if this entry is no longer claimable.
        store_module._write_state(state)
        journal = state.get("call_retry_journal", {})
        entry = journal.get(retry_id)
        if not isinstance(entry, dict) or entry.get("status") != "pending":
            return {}
        if float(entry.get("next_attempt_at") or 0.0) > now:
            return {}
        lane = str(entry.get("lane") or payloads_module._call_retry_lane(entry["reference"]))
        lane_entries = [
            candidate
            for candidate in journal.values()
            if isinstance(candidate, dict)
            and str(
                candidate.get("lane")
                or payloads_module._call_retry_lane(candidate.get("reference") or {})
            )
            == lane
            and (
                candidate.get("status", "pending") == "pending"
                or (
                    candidate.get("status") == "dead_letter"
                    and candidate.get("operation") == "create"
                )
            )
        ]
        first = min(
            lane_entries,
            key=lambda candidate: (
                int(candidate.get("sequence") or 0),
                float(candidate.get("created_at") or 0.0),
                str(candidate.get("retry_id") or ""),
            ),
            default=None,
        )
        if not isinstance(first, dict) or first.get("retry_id") != retry_id:
            return {}
        if float(entry.get("lease_expires_at") or 0.0) > now:
            return {}
        lease_token = uuid.uuid4().hex
        entry.update(
            {
                "lane": lane,
                "lease_owner": owner,
                "lease_token": lease_token,
                "lease_expires_at": now + constants.CALL_RETRY_LEASE_SECONDS,
            }
        )
        store_module._write_state(state)
        return deepcopy(entry)


def _safe_retry_error(exc: Exception) -> tuple[str, str]:
    """Return body-free error metadata; never retain command or payload text."""
    error_type = type(exc).__name__[:80] or "Exception"
    # Patch mutations expose the normalized ``HTTP NNN`` wrapper, while call
    # creates still surface the Interface CLI's ``api NNN`` prefix. Parse
    # either spelling so permanent create failures do not consume the full
    # transient retry budget.
    status_match = re.search(
        r"\b(?:HTTP|api)\s+([1-5][0-9]{2})\b",
        str(exc),
        flags=re.IGNORECASE,
    )
    status = status_match.group(1) if status_match else ""
    code = f"{error_type}:http_{status}" if status else error_type
    return error_type, code


def _terminal_call_retry_error(exc: Exception) -> bool:
    if isinstance(exc, (TypeError, ValueError, constants.WorkUpdateError)):
        return True
    _error_type, code = _safe_retry_error(exc)
    match = re.search(r"http_([1-5][0-9]{2})$", code)
    return bool(
        match
        and int(match.group(1))
        in {400, 404, 405, 410, 413, 422}
    )


def _record_call_retry_failure(
    retry_id: str,
    entry: dict[str, Any],
    exc: Exception,
) -> None:
    now = time.time()
    error_type, safe_error = _safe_retry_error(exc)
    updated: dict[str, Any] = {}
    with store_module._state_guard():
        state = store_module._read_state()
        current = state.get("call_retry_journal", {}).get(retry_id)
        if (
            isinstance(current, dict)
            and current.get("lease_token") == entry.get("lease_token")
        ):
            attempts = max(0, int(current.get("attempts") or 0)) + 1
            terminal = (
                _terminal_call_retry_error(exc)
                or attempts >= constants.CALL_RETRY_MAX_ATTEMPTS
            )
            base_delay = min(
                constants.CALL_RETRY_BASE_DELAY_SECONDS
                * (2 ** min(attempts - 1, 12)),
                constants.CALL_RETRY_MAX_DELAY_SECONDS,
            )
            delay = min(
                base_delay * random.uniform(0.75, 1.25),
                constants.CALL_RETRY_MAX_DELAY_SECONDS,
            )
            current.update(
                {
                    "status": "dead_letter" if terminal else "pending",
                    "attempts": attempts,
                    "last_attempt_at": now,
                    "next_attempt_at": 0.0 if terminal else now + delay,
                    "last_error": safe_error,
                    "dead_lettered_at": now if terminal else 0.0,
                    "lease_owner": "",
                    "lease_token": "",
                    "lease_expires_at": 0.0,
                }
            )
            updated = deepcopy(current)
            store_module._write_state(state)
    if not updated:
        return
    reference = updated.get("reference") or {}
    owner = str(reference.get("owner_contact_id") or "")
    direction = str(updated.get("direction") or "")
    call_id = str(reference.get("call_id") or "")
    attempts = int(updated.get("attempts") or 0)
    terminal = updated.get("status") == "dead_letter"
    delay = max(0.0, float(updated.get("next_attempt_at") or 0.0) - now)
    disposition = (
        "moved to dead letter"
        if terminal
        else f"retrying in {delay:.1f}s"
    )
    print(
        "[Work updates] call card delivery failed "
        f"(direction={direction}, call_id={call_id}, attempt={attempts}, "
        f"error_type={error_type}); {disposition}",
        flush=True,
    )
    try:
        from diagnostics.store import Diagnostics

        trace = Diagnostics.get_active_run(owner)
        if trace is not None:
            trace.event(
                "work.call_delivery_failed",
                direction=direction,
                call_id=call_id,
                attempt=attempts,
                retry_after_seconds=delay,
                error_type=error_type,
                terminal=terminal,
            )
    except Exception:
        pass


def _complete_call_retry(retry_id: str, entry: dict[str, Any]) -> bool:
    removed = False
    with store_module._state_guard():
        state = store_module._read_state()
        journal = state.get("call_retry_journal", {})
        current = journal.get(retry_id)
        if (
            isinstance(current, dict)
            and current.get("lease_token") == entry.get("lease_token")
        ):
            journal.pop(retry_id, None)
            removed = True
            store_module._write_state(state)
    attempts = max(0, int(entry.get("attempts") or 0))
    if not removed or not attempts:
        return removed
    reference = entry.get("reference") if isinstance(entry, dict) else {}
    owner = str((reference or {}).get("owner_contact_id") or "")
    direction = str(entry.get("direction") or "")
    call_id = str((reference or {}).get("call_id") or "")
    print(
        "[Work updates] call card delivery recovered "
        f"(direction={direction}, call_id={call_id}, attempts={attempts + 1})",
        flush=True,
    )
    try:
        from diagnostics.store import Diagnostics

        trace = Diagnostics.get_active_run(owner)
        if trace is not None:
            trace.event(
                "work.call_delivery_recovered",
                direction=direction,
                call_id=call_id,
                attempts=attempts + 1,
            )
    except Exception:
        pass
    return removed
