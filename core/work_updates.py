"""Durable work-update orchestration for Stemcell managers.

Glass owns canonical task state, timing, history, and chat events.  This module
only keeps the small amount of local correlation needed to reuse accepted
identities across manager rounds, workers, and progress frames.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from core.background import submit_best_effort
from core.interface import (
    InterfaceClient,
    InterfaceError,
    STATE_DIR,
    WorkCallMutationError,
    get_contact,
    get_own_profile,
)
from core.state_store import file_lock, read_json, write_json


WORK_UPDATES_FILE = STATE_DIR / "work_updates.json"
_STATE_LOCK = threading.RLock()
_CALL_RETRY_LOCK = threading.Lock()
_CALL_RETRY_INFLIGHT: set[str] = set()
_CALL_RETRY_PROCESS_PID = os.getpid()
_CALL_RETRY_PROCESS_TOKEN = uuid.uuid4().hex
PENDING_CALL_TTL_SECONDS = 6 * 60 * 60
CALL_IDLE_TIMEOUT_SECONDS = 10.0
TERMINAL_TASK_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_CACHED_TASKS_PER_CONTACT = 200
CALL_RETRY_BASE_DELAY_SECONDS = 1.0
CALL_RETRY_MAX_DELAY_SECONDS = 5 * 60.0
CALL_RETRY_BATCH_LIMIT = 20
# After the short exponential ramp, transient delivery remains live for about
# 24 hours at the five-minute ceiling before moving to the bounded dead letter.
CALL_RETRY_MAX_ATTEMPTS = 297
CALL_RETRY_LEASE_SECONDS = 90.0
CALL_RETRY_MAX_ENTRIES = 1_000
CALL_RETRY_DEAD_LETTER_RETENTION_SECONDS = 24 * 60 * 60
CALL_RETRY_ARCHIVE_LIMIT = 200
CALL_RETRY_DEDUPE_RETENTION_SECONDS = 7 * 24 * 60 * 60
CALL_RETRY_DEDUPE_LIMIT = 5_000

CANONICAL_ACTIVITY_STATES = {
    "thinking",
    "reading",
    "writing",
    "executing",
    "searching_web",
    "spawning_worker",
    "calling",
    "other",
    "done",
}
ACTIVITY_STATE_ALIASES = {
    "reading_file": "reading",
    "writing_file": "writing",
}
TERMINAL_ACTIONS = {
    "task/complete": ("complete", "completion", "completed"),
    "task/fail": ("fail", "failure", "failed"),
    "task/cancel": ("cancel", "cancellation", "cancelled"),
}
WORKER_STATES = {
    "yet_to_start",
    "in_progress",
    "completed",
    "blocked",
    "failed",
    "cancelled",
}
CALL_STATES = {"connecting", "in_progress", "completed", "failed", "cancelled"}


class WorkUpdateError(RuntimeError):
    """A manager-visible durable update failure."""


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "contacts": {},
        "call_retry_journal": {},
        "call_retry_sequence": 0,
        "call_retry_dead_letters": [],
        "call_retry_overflow_count": 0,
        "call_retry_last_overflow_at": 0.0,
        "call_retry_dedupe": {},
    }


def _read_state() -> dict[str, Any]:
    state = read_json(WORK_UPDATES_FILE, _default_state())
    if not isinstance(state, dict):
        return _default_state()
    state.setdefault("version", 1)
    state.setdefault("contacts", {})
    state.setdefault("call_retry_journal", {})
    state.setdefault("call_retry_sequence", 0)
    state.setdefault("call_retry_dead_letters", [])
    state.setdefault("call_retry_overflow_count", 0)
    state.setdefault("call_retry_last_overflow_at", 0.0)
    state.setdefault("call_retry_dedupe", {})
    _prune_state(state)
    return state


def _write_state(state: dict[str, Any]) -> None:
    write_json(WORK_UPDATES_FILE, state)


@contextmanager
def _state_guard():
    with _STATE_LOCK, file_lock(WORK_UPDATES_FILE):
        yield


def _timestamp(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _call_retry_archive_record(entry: dict[str, Any], now: float) -> dict[str, Any]:
    reference = entry.get("reference") or {}
    identity = "\x1f".join(
        (
            str(reference.get("owner_contact_id") or ""),
            str(reference.get("task_id") or ""),
            str(reference.get("call_id") or ""),
        )
    )
    return {
        "retry_id": str(entry.get("retry_id") or ""),
        "operation": str(entry.get("operation") or ""),
        "direction": str(entry.get("direction") or ""),
        "call_ref": hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        "attempts": int(entry.get("attempts") or 0),
        "last_error": str(entry.get("last_error") or "")[:120],
        "archived_at": now,
    }


def _prune_state(state: dict[str, Any], now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    dedupe = state.get("call_retry_dedupe")
    if not isinstance(dedupe, dict):
        state["call_retry_dedupe"] = {}
    else:
        for key, receipt in list(dedupe.items()):
            if (
                not isinstance(receipt, dict)
                or now - float(receipt.get("created_at") or 0.0)
                >= CALL_RETRY_DEDUPE_RETENTION_SECONDS
            ):
                dedupe.pop(key, None)
        if len(dedupe) > CALL_RETRY_DEDUPE_LIMIT:
            ordered = sorted(
                dedupe,
                key=lambda key: float(
                    (dedupe.get(key) or {}).get("created_at") or 0.0
                ),
            )
            for key in ordered[: len(dedupe) - CALL_RETRY_DEDUPE_LIMIT]:
                dedupe.pop(key, None)
    journal = state.get("call_retry_journal")
    pending_call_ids: set[str] = set()
    if not isinstance(journal, dict):
        state["call_retry_journal"] = {}
    else:
        for retry_id, entry in list(journal.items()):
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("reference"), dict)
                or not str(entry["reference"].get("call_id") or "")
                or entry.get("direction")
                not in {"outbound", "inbound", "mutation"}
            ):
                journal.pop(retry_id, None)
                continue
            if (
                entry.get("status") == "dead_letter"
                and now
                - float(
                    entry.get("dead_lettered_at")
                    or entry.get("last_attempt_at")
                    or entry.get("created_at")
                    or now
                )
                >= CALL_RETRY_DEAD_LETTER_RETENTION_SECONDS
            ):
                archive = state.setdefault("call_retry_dead_letters", [])
                if not isinstance(archive, list):
                    archive = []
                    state["call_retry_dead_letters"] = archive
                archive.append(_call_retry_archive_record(entry, now))
                del archive[:-CALL_RETRY_ARCHIVE_LIMIT]
                journal.pop(retry_id, None)
                continue
            if (
                entry.get("status", "pending") == "pending"
                or (
                    entry.get("status") == "dead_letter"
                    and entry.get("operation") == "create"
                )
            ):
                pending_call_ids.add(str(entry["reference"]["call_id"]))
    for contact in state.get("contacts", {}).values():
        if not isinstance(contact, dict):
            continue
        pending = contact.get("pending_calls")
        if isinstance(pending, dict):
            for peer_id, correlation in list(pending.items()):
                if not isinstance(correlation, dict):
                    pending.pop(peer_id, None)
                    continue
                updated_at = _timestamp(correlation.get("updated_at"))
                correlation_call_ids = {
                    str(correlation.get("outbound_call_id") or ""),
                    str(correlation.get("inbound_call_id") or ""),
                }
                if (
                    not correlation_call_ids.intersection(pending_call_ids)
                    and (
                        correlation.get("terminal_requested")
                        or not updated_at
                        or now - updated_at > PENDING_CALL_TTL_SECONDS
                    )
                ):
                    pending.pop(peer_id, None)

        standalone_calls = contact.get("standalone_calls")
        if isinstance(standalone_calls, dict):
            for call_id, call in list(standalone_calls.items()):
                if not isinstance(call, dict):
                    standalone_calls.pop(call_id, None)
                    continue
                cached_at = _timestamp(call.get("_cached_at"))
                if (
                    call.get("state") in {"completed", "failed", "cancelled"}
                    and cached_at
                    and now - cached_at > TERMINAL_TASK_TTL_SECONDS
                ):
                    standalone_calls.pop(call_id, None)
            if len(standalone_calls) > 200:
                ordered = sorted(
                    (
                        (_timestamp(call.get("_cached_at")), call_id)
                        for call_id, call in standalone_calls.items()
                        if isinstance(call, dict)
                    )
                )
                for _cached_at, call_id in ordered[: len(standalone_calls) - 200]:
                    standalone_calls.pop(call_id, None)

        tasks = contact.get("tasks")
        if not isinstance(tasks, dict):
            continue
        terminal = []
        for task_id, task in list(tasks.items()):
            if not isinstance(task, dict):
                tasks.pop(task_id, None)
                continue
            cached_at = _timestamp(task.get("_cached_at"))
            if (
                task.get("state") in {"completed", "failed", "cancelled"}
                and cached_at
                and now - cached_at > TERMINAL_TASK_TTL_SECONDS
            ):
                tasks.pop(task_id, None)
                continue
            for bucket_name, limit in (
                ("events", 500),
                ("calls", 200),
                ("worker_groups", 200),
                ("todos", 500),
            ):
                bucket = task.get(bucket_name)
                if isinstance(bucket, dict) and len(bucket) > limit:
                    for stale_id in list(bucket)[: len(bucket) - limit]:
                        bucket.pop(stale_id, None)
            terminal.append((cached_at, str(task_id)))
        if len(tasks) > MAX_CACHED_TASKS_PER_CONTACT:
            active_id = str(contact.get("active_task_id") or "")
            for _cached_at, task_id in sorted(terminal):
                if len(tasks) <= MAX_CACHED_TASKS_PER_CONTACT:
                    break
                if task_id != active_id:
                    tasks.pop(task_id, None)


def _contact_state(state: dict[str, Any], contact_id: str) -> dict[str, Any]:
    contacts = state.setdefault("contacts", {})
    contact = contacts.setdefault(
        contact_id,
        {
            "active_task_id": "",
            "activity": {},
            "tasks": {},
            "workers": {},
            "pending_calls": {},
            "standalone_calls": {},
        },
    )
    contact.setdefault("active_task_id", "")
    contact.setdefault("activity", {})
    contact.setdefault("tasks", {})
    contact.setdefault("workers", {})
    contact.setdefault("pending_calls", {})
    contact.setdefault("standalone_calls", {})
    return contact


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


def _call_retry_id(direction: str, reference: dict[str, Any]) -> str:
    call_id = str(reference.get("call_id") or "")
    return _stable_id(
        "call-create-retry",
        direction,
        reference.get("owner_contact_id"),
        call_id,
    )


def _call_retry_lane(reference: dict[str, Any]) -> str:
    return _stable_id(
        "call-lane",
        reference.get("owner_contact_id"),
        reference.get("task_id"),
        reference.get("call_id"),
    )


def _transcript_timestamps(
    transcript: Any,
    *,
    occurred_at: str = "",
) -> list[dict[str, Any]]:
    """Freeze server-default transcript timestamps before first delivery."""
    if not isinstance(transcript, list):
        return []
    fallback = str(occurred_at or _utc_now())
    result: list[dict[str, Any]] = []
    for value in transcript:
        if not isinstance(value, dict):
            continue
        row = deepcopy(value)
        created_at = str(row.get("created_at") or fallback)
        row["created_at"] = created_at
        row["updated_at"] = str(row.get("updated_at") or created_at)
        result.append(row)
    return result


def _call_create_payload(
    direction: str,
    reference: dict[str, Any],
    *,
    target_name: str = "",
    message: str = "",
    occurred_at: str = "",
) -> dict[str, Any]:
    owner = str(reference.get("owner_contact_id") or "")
    task_id = str(reference.get("task_id") or "")
    call_id = str(reference.get("call_id") or "")
    event_id = str(reference.get("work_event_id") or "")
    if not owner or not call_id:
        raise WorkUpdateError("Call retry requires an owner and call_id.")
    occurred_at = str(occurred_at or _utc_now())
    if direction == "outbound":
        target_id = str(reference.get("target_id") or "")
        target_kind = (
            "silicon"
            if str(reference.get("target_kind") or "") == "silicon"
            else "manager"
        )
        name = str(target_name or target_id)
        speaker_name = str(
            reference.get("speaker_name")
            or get_own_profile().get("name")
            or "Silicon manager"
        )
        transcript = [
            {
                "transcript_id": _stable_id("transcript-outbound", call_id),
                "speaker_kind": "manager",
                "speaker_id": _safe_fragment(f"manager:{owner}", "manager"),
                "speaker_name": speaker_name,
                "body": str(message or ""),
                "blocks": [],
                "revision": 0,
                "created_at": occurred_at,
                "updated_at": occurred_at,
            }
        ]
        body = (
            f"Called {name}"
            if target_kind == "silicon"
            else f"Calling {name}"
        )
    elif direction == "inbound":
        target_id = str(reference.get("source_id") or "")
        target_kind = (
            "silicon"
            if str(reference.get("source_kind") or "") == "silicon"
            else "manager"
        )
        name = str(reference.get("source_name") or target_id)
        transcript = [
            {
                "transcript_id": _stable_id("transcript-inbound", call_id),
                "speaker_kind": target_kind,
                "speaker_id": _safe_fragment(
                    (
                        target_id
                        if target_kind == "silicon"
                        else f"manager:{target_id}"
                    ),
                    "speaker",
                ),
                "speaker_name": name,
                "body": str(reference.get("message") or ""),
                "blocks": [],
                "revision": 0,
                "created_at": occurred_at,
                "updated_at": occurred_at,
            }
        ]
        body = f"Received call from {name}"
    else:
        raise ValueError("call retry direction is invalid")
    payload = {
        "call_id": call_id,
        "work_event_id": event_id
        or _stable_id("call-event", owner, task_id, call_id),
        "kind": "call",
        "direction": direction,
        "target_kind": target_kind,
        "target_id": _safe_fragment(target_id, "unknown"),
        "target_name": name,
        "state": "in_progress",
        "body": body,
        "blocks": [],
        "transcript": transcript,
        "client_id": _stable_id(
            "create-call",
            task_id or str(reference.get("room_id") or ""),
            call_id,
        ),
    }
    if not task_id:
        room_id = str(
            reference.get("room_id")
            or (get_contact(owner) or {}).get("room_id")
            or ""
        )
        if not room_id:
            raise WorkUpdateError("Standalone call retry requires a room.")
        payload["room_id"] = room_id
    return payload


def _new_call_retry_entry(
    *,
    retry_id: str,
    operation: str,
    direction: str,
    reference: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    now = time.time()
    return {
        "retry_id": retry_id,
        "operation": operation,
        "direction": direction,
        "reference": deepcopy(reference),
        "payload": deepcopy(payload),
        "lane": _call_retry_lane(reference),
        "status": "pending",
        "attempts": 0,
        "created_at": now,
        "last_attempt_at": 0.0,
        "next_attempt_at": 0.0,
        "last_error": "",
        "lease_owner": "",
        "lease_token": "",
        "lease_expires_at": 0.0,
    }


def _call_retry_capacity_victim(
    journal: dict[str, Any],
    *,
    now: float,
) -> dict[str, Any] | None:
    """Choose one safe, already-failed retry to archive under backpressure."""
    dead_letters = [
        entry
        for entry in journal.values()
        if isinstance(entry, dict) and entry.get("status") == "dead_letter"
    ]
    if dead_letters:
        return min(
            dead_letters,
            key=lambda entry: (
                float(entry.get("dead_lettered_at") or 0.0),
                int(entry.get("sequence") or 0),
            ),
        )

    failed = [
        entry
        for entry in journal.values()
        if (
            isinstance(entry, dict)
            and entry.get("status", "pending") == "pending"
            and str(entry.get("last_error") or "")
            and float(entry.get("lease_expires_at") or 0.0) <= now
        )
    ]
    if not failed:
        return None
    return min(
        failed,
        key=lambda entry: (
            float(entry.get("created_at") or 0.0),
            int(entry.get("sequence") or 0),
        ),
    )


def _insert_call_retry_entries_in_state(
    state: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    dedupe_key: str = "",
    dedupe_result: dict[str, Any] | None = None,
    dedupe_kind: str = "",
) -> list[str]:
    """Mutate a locked state with one or more call retry entries."""
    inserted: list[str] = []
    dedupe = state.setdefault("call_retry_dedupe", {})
    if dedupe_key and isinstance(dedupe.get(dedupe_key), dict):
        return [
            str(value)
            for value in dedupe[dedupe_key].get("retry_ids") or []
            if value
        ]
    journal = state.setdefault("call_retry_journal", {})
    sequence = int(state.get("call_retry_sequence") or 0)
    new_count = sum(
        1
        for source in entries
        if str(source.get("retry_id") or "")
        and not isinstance(
            journal.get(str(source.get("retry_id") or "")),
            dict,
        )
    )
    while len(journal) + new_count > CALL_RETRY_MAX_ENTRIES:
        now = time.time()
        victim = _call_retry_capacity_victim(journal, now=now)
        if not isinstance(victim, dict):
            state["call_retry_overflow_count"] = (
                int(state.get("call_retry_overflow_count") or 0) + 1
            )
            state["call_retry_last_overflow_at"] = now
            raise WorkUpdateError(
                "Call retry journal is at its live-entry limit."
            )
        if victim.get("status") != "dead_letter":
            state["call_retry_overflow_count"] = (
                int(state.get("call_retry_overflow_count") or 0) + 1
            )
            state["call_retry_last_overflow_at"] = now
            prior_error = str(victim.get("last_error") or "")[:96]
            victim["status"] = "dead_letter"
            victim["dead_lettered_at"] = now
            victim["last_error"] = (
                f"{prior_error}|capacity_evicted"
                if prior_error
                else "capacity_evicted"
            )
        archive = state.setdefault("call_retry_dead_letters", [])
        if not isinstance(archive, list):
            archive = []
            state["call_retry_dead_letters"] = archive
        archive.append(_call_retry_archive_record(victim, now))
        del archive[:-CALL_RETRY_ARCHIVE_LIMIT]
        journal.pop(str(victim.get("retry_id") or ""), None)
    for source in entries:
        retry_id = str(source.get("retry_id") or "")
        if not retry_id:
            raise WorkUpdateError("Call journal entry requires retry_id.")
        if isinstance(journal.get(retry_id), dict):
            inserted.append(retry_id)
            continue
        sequence += 1
        entry = deepcopy(source)
        entry["sequence"] = sequence
        journal[retry_id] = entry
        inserted.append(retry_id)
    state["call_retry_sequence"] = sequence
    if dedupe_key:
        dedupe[dedupe_key] = {
            "retry_ids": list(inserted),
            "created_at": time.time(),
            **({"kind": dedupe_kind} if dedupe_kind else {}),
            **(
                {"result": deepcopy(dedupe_result)}
                if isinstance(dedupe_result, dict)
                else {}
            ),
        }
    return inserted


def _insert_call_retry_entries(
    entries: list[dict[str, Any]],
    *,
    dedupe_key: str = "",
    dedupe_result: dict[str, Any] | None = None,
    dedupe_kind: str = "",
) -> list[str]:
    """Atomically persist one or more independent call-side mutations."""
    with _state_guard():
        state = _read_state()
        try:
            inserted = _insert_call_retry_entries_in_state(
                state,
                entries,
                dedupe_key=dedupe_key,
                dedupe_result=dedupe_result,
                dedupe_kind=dedupe_kind,
            )
        finally:
            # Preserve overflow/dead-letter accounting even when insertion
            # cannot accept another live mutation.
            _write_state(state)
    return inserted


def _call_retry_dedupe_receipt(dedupe_key: str) -> dict[str, Any]:
    """Return one body-free idempotency receipt, including legacy kind inference."""
    if not dedupe_key:
        return {}
    with _state_guard():
        state = _read_state()
        receipt = state.get("call_retry_dedupe", {}).get(dedupe_key)
        if not isinstance(receipt, dict):
            return {}
        result = deepcopy(receipt)
        if not result.get("kind"):
            operations = {
                str(entry.get("operation") or "")
                for retry_id in result.get("retry_ids") or []
                if isinstance(
                    entry := state.get("call_retry_journal", {}).get(
                        str(retry_id or "")
                    ),
                    dict,
                )
            }
            if operations == {"create"}:
                result["kind"] = "create"
            elif operations:
                result["kind"] = "append"
        return result


def _call_retry_dedupe_result(
    dedupe_key: str,
    retry_id: str = "",
) -> dict[str, Any]:
    """Return the canonical result retained for a replayed ingress intent."""
    with _state_guard():
        state = _read_state()
        receipt = (
            state.get("call_retry_dedupe", {}).get(dedupe_key)
            if dedupe_key
            else None
        )
        if isinstance(receipt, dict) and isinstance(
            receipt.get("result"),
            dict,
        ):
            return deepcopy(receipt["result"])
        entry = state.get("call_retry_journal", {}).get(retry_id)
        if isinstance(entry, dict) and isinstance(entry.get("reference"), dict):
            return deepcopy(entry["reference"])
    return {}


def _journal_call_create(
    direction: str,
    reference: dict[str, Any],
    *,
    target_name: str = "",
    message: str = "",
    dedupe_key: str = "",
) -> str:
    """Durably retain one byte-stable call create before network I/O."""
    stable_reference = deepcopy(reference)
    owner = str(stable_reference.get("owner_contact_id") or "")
    if not stable_reference.get("room_id") and owner:
        stable_reference["room_id"] = str(
            (get_contact(owner) or {}).get("room_id") or ""
        )
    if direction == "outbound":
        stable_reference["speaker_name"] = str(
            get_own_profile().get("name") or "Silicon manager"
        )
    occurred_at = _utc_now()
    retry_id = _call_retry_id(direction, stable_reference)
    payload = _call_create_payload(
        direction,
        stable_reference,
        target_name=target_name,
        message=message,
        occurred_at=occurred_at,
    )
    entry = _new_call_retry_entry(
        retry_id=retry_id,
        operation="create",
        direction=direction,
        reference=stable_reference,
        payload=payload,
    )
    # The live journal necessarily retains the transcript until delivery.
    # The longer-lived idempotency receipt only needs correlation identity.
    receipt_reference = {
        key: deepcopy(value)
        for key, value in stable_reference.items()
        if key not in {"message", "source_name", "speaker_name", "target_name"}
    }
    return _insert_call_retry_entries(
        [entry],
        dedupe_key=dedupe_key,
        dedupe_result=receipt_reference,
        dedupe_kind="create",
    )[0]


def _call_patch_entry(
    reference: dict[str, Any],
    payload: dict[str, Any],
    *,
    mutation_id: str,
    direction: str = "mutation",
    occurred_at: str = "",
) -> dict[str, Any]:
    owner = str(reference.get("owner_contact_id") or "")
    call_id = str(reference.get("call_id") or "")
    if not owner or not call_id:
        raise WorkUpdateError("Call mutation requires an owner and call_id.")
    occurred_at = str(occurred_at or _utc_now())
    stable_payload = deepcopy(payload)
    stable_payload.pop("revision", None)
    if "transcript" in stable_payload:
        stable_payload["transcript"] = _transcript_timestamps(
            stable_payload.get("transcript"),
            occurred_at=occurred_at,
        )
    stable_payload["client_id"] = _stable_id(
        "call-mutation",
        owner,
        call_id,
        mutation_id,
    )
    retry_id = _stable_id(
        "call-mutation-retry",
        owner,
        reference.get("task_id"),
        call_id,
        mutation_id,
    )
    return _new_call_retry_entry(
        retry_id=retry_id,
        operation="patch",
        direction=direction,
        reference=reference,
        payload=stable_payload,
    )


def _journal_call_patch(
    reference: dict[str, Any],
    payload: dict[str, Any],
    *,
    mutation_id: str = "",
    direction: str = "mutation",
    occurred_at: str = "",
) -> str:
    mutation_id = str(
        mutation_id or _new_id("call-mutation", reference.get("call_id"))
    )
    entry = _call_patch_entry(
        reference,
        payload,
        mutation_id=mutation_id,
        direction=direction,
        occurred_at=occurred_at,
    )
    return _insert_call_retry_entries([entry])[0]


def _call_retry_entry(retry_id: str) -> dict[str, Any]:
    with _state_guard():
        state = _read_state()
        return deepcopy(state.get("call_retry_journal", {}).get(retry_id) or {})


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
    if isinstance(exc, (TypeError, ValueError, WorkUpdateError)):
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
    with _state_guard():
        state = _read_state()
        current = state.get("call_retry_journal", {}).get(retry_id)
        if (
            isinstance(current, dict)
            and current.get("lease_token") == entry.get("lease_token")
        ):
            attempts = max(0, int(current.get("attempts") or 0)) + 1
            terminal = (
                _terminal_call_retry_error(exc)
                or attempts >= CALL_RETRY_MAX_ATTEMPTS
            )
            base_delay = min(
                CALL_RETRY_BASE_DELAY_SECONDS
                * (2 ** min(attempts - 1, 12)),
                CALL_RETRY_MAX_DELAY_SECONDS,
            )
            delay = min(
                base_delay * random.uniform(0.75, 1.25),
                CALL_RETRY_MAX_DELAY_SECONDS,
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
            _write_state(state)
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
        from core.diagnostics import Diagnostics

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
    with _state_guard():
        state = _read_state()
        journal = state.get("call_retry_journal", {})
        current = journal.get(retry_id)
        if (
            isinstance(current, dict)
            and current.get("lease_token") == entry.get("lease_token")
        ):
            journal.pop(retry_id, None)
            removed = True
            _write_state(state)
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
        from core.diagnostics import Diagnostics

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


def _has_call_reference(correlation: dict[str, Any], prefix: str) -> bool:
    return bool(
        correlation.get(f"{prefix}_owner_contact_id")
        and correlation.get(f"{prefix}_call_id")
    )


def _call_reference_for_owner(
    correlation: dict[str, Any],
    contact_id: str,
) -> dict[str, str]:
    """Return only the call card owned by this manager/contact."""
    prefix = _call_role_for_owner(correlation, contact_id)
    if prefix:
        return {
            "owner_contact_id": str(
                correlation.get(f"{prefix}_owner_contact_id") or ""
            ),
            "task_id": str(correlation.get(f"{prefix}_task_id") or ""),
            "call_id": str(correlation.get(f"{prefix}_call_id") or ""),
            "work_event_id": str(
                correlation.get(f"{prefix}_work_event_id") or ""
            ),
            "continuation_role": prefix,
        }
    return {}


def _call_role_for_owner(
    correlation: dict[str, Any],
    contact_id: str,
) -> str:
    """Return whether this contact owns the outbound or inbound mirror."""
    for prefix in ("outbound", "inbound"):
        if (
            _has_call_reference(correlation, prefix)
            and str(correlation.get(f"{prefix}_owner_contact_id") or "")
            == str(contact_id)
        ):
            return prefix
    return ""


def canonical_activity_state(state: str) -> str:
    normalized = ACTIVITY_STATE_ALIASES.get(str(state or ""), str(state or ""))
    return normalized if normalized in CANONICAL_ACTIVITY_STATES else "other"


def begin_manager_activity(contact_id: str, run_id: str = "") -> str:
    """Start or recover the stable manager-activity group for one inbound run."""
    seed = str(run_id or uuid.uuid4().hex)
    group_id = f"manager-run:{_safe_fragment(seed, uuid.uuid4().hex)}"
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        current = contact.get("activity")
        if not isinstance(current, dict) or current.get("run_id") != seed:
            contact["activity"] = {
                "run_id": seed,
                "group_id": group_id,
                "sequence": 0,
                "frames": {},
                "settled": False,
            }
            _write_state(state)
        else:
            group_id = str(current.get("group_id") or group_id)
    touch_manager_call_activity(contact_id)
    return group_id


def current_manager_activity_group(contact_id: str) -> str:
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        activity = contact.get("activity")
        if not isinstance(activity, dict) or activity.get("settled"):
            return ""
        return str(activity.get("group_id") or "")


def activity_frame_identity(
    contact_id: str,
    group_id: str,
    *,
    frame_key: str = "",
    fingerprint: str = "",
) -> tuple[str, int, bool]:
    """Return (frame_id, revision, duplicate).

    A provider item keeps the same frame while its accepted representation
    changes.  Exact retries keep the same revision so Glass can replay them
    idempotently.
    """
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        activity = contact.setdefault("activity", {})
        if activity.get("group_id") != group_id:
            activity.clear()
            activity.update(
                {
                    "run_id": group_id,
                    "group_id": group_id,
                    "sequence": 0,
                    "frames": {},
                    "settled": False,
                }
            )
        frames = activity.setdefault("frames", {})
        if frame_key:
            key = _stable_id("frame-key", frame_key)
        else:
            activity["sequence"] = int(activity.get("sequence") or 0) + 1
            key = f"sequence:{activity['sequence']}"
        frame = frames.get(key)
        duplicate = False
        if not isinstance(frame, dict):
            frame = {
                "frame_id": _stable_id("activity", group_id, key),
                "revision": 0,
                "fingerprint": fingerprint,
            }
            frames[key] = frame
        elif fingerprint and frame.get("fingerprint") == fingerprint:
            duplicate = True
        else:
            frame["revision"] = int(frame.get("revision") or 0) + 1
            frame["fingerprint"] = fingerprint
        if len(frames) > 500:
            for stale_key in list(frames)[: len(frames) - 500]:
                if stale_key != key:
                    frames.pop(stale_key, None)
        _write_state(state)
        return (
            str(frame["frame_id"]),
            int(frame.get("revision") or 0),
            duplicate,
        )


def settle_manager_activity(contact_id: str, group_id: str) -> None:
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        activity = contact.get("activity")
        if isinstance(activity, dict) and activity.get("group_id") == group_id:
            activity["settled"] = True
            _write_state(state)


def active_task_id(contact_id: str) -> str:
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        return str(contact.get("active_task_id") or "")


def set_active_task_timer(
    contact_id: str,
    *,
    timer_state: str,
    pause_reason: str | None = None,
    client: InterfaceClient | None = None,
) -> bool:
    """Best-effort external pause/resume using Glass-owned elapsed time."""
    task_id = active_task_id(contact_id)
    if not task_id:
        return False
    cached = _task_cache(contact_id, task_id)
    if cached.get("state") not in {"queued", "running"}:
        return False
    if timer_state == "paused" and pause_reason not in {
        "rate_limited",
        "offline",
        "infrastructure",
    }:
        return False
    if timer_state == "running" and cached.get("timer_state") != "paused":
        return False
    payload: dict[str, Any] = {"timer_state": timer_state}
    if timer_state == "paused":
        payload["timer_pause_reason"] = pause_reason
    else:
        payload["timer_pause_reason"] = None
    try:
        WorkUpdates(contact_id, client=client).execute(
            {
                "action": "task/update",
                "task_id": task_id,
                "data": payload,
            }
        )
    except Exception:
        return False
    return True


def _task_cache(contact_id: str, task_id: str) -> dict[str, Any]:
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        task = contact["tasks"].get(task_id)
        return deepcopy(task) if isinstance(task, dict) else {}


def _standalone_call_cache(contact_id: str, call_id: str) -> dict[str, Any]:
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        call = contact["standalone_calls"].get(call_id)
        return deepcopy(call) if isinstance(call, dict) else {}


def _remember_task(contact_id: str, snapshot: dict[str, Any]) -> None:
    task_id = str(snapshot.get("task_id") or "")
    if not task_id:
        return
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        previous = contact["tasks"].get(task_id)
        cached = dict(previous) if isinstance(previous, dict) else {}
        for key in (
            "task_id",
            "room_id",
            "title",
            "description",
            "state",
            "revision",
            "estimate_seconds",
            "active_elapsed_seconds",
            "timer_state",
            "timer_pause_reason",
        ):
            if key in snapshot:
                cached[key] = deepcopy(snapshot[key])
        todos = cached.setdefault("todos", {})
        for todo in snapshot.get("todos") or []:
            if isinstance(todo, dict) and todo.get("todo_id"):
                todos[str(todo["todo_id"])] = deepcopy(todo)
        cached.setdefault("events", {})
        cached.setdefault("worker_groups", {})
        cached.setdefault("calls", {})
        cached["_cached_at"] = time.time()
        contact["tasks"][task_id] = cached
        if snapshot.get("state") not in {"completed", "failed", "cancelled"}:
            contact["active_task_id"] = task_id
        elif contact.get("active_task_id") == task_id:
            contact["active_task_id"] = ""
        _write_state(state)


def _remember_event(contact_id: str, snapshot: dict[str, Any]) -> None:
    task_id = str(snapshot.get("task_id") or "")
    kind = str(snapshot.get("kind") or "")
    cached_at = time.time()
    if not task_id and kind == "call" and snapshot.get("call_id"):
        with _state_guard():
            state = _read_state()
            contact = _contact_state(state, contact_id)
            call = deepcopy(snapshot)
            call["_cached_at"] = cached_at
            contact["standalone_calls"][str(snapshot["call_id"])] = call
            _write_state(state)
        return
    if not task_id:
        return
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        task = contact["tasks"].setdefault(
            task_id,
            {
                "task_id": task_id,
                "todos": {},
                "events": {},
                "worker_groups": {},
                "calls": {},
            },
        )
        task.setdefault("events", {})
        task.setdefault("worker_groups", {})
        task.setdefault("calls", {})
        work_event_id = str(snapshot.get("work_event_id") or "")
        if work_event_id:
            task["events"][work_event_id] = deepcopy(snapshot)
        if kind == "worker_group" and snapshot.get("group_id"):
            task["worker_groups"][str(snapshot["group_id"])] = deepcopy(snapshot)
        if kind == "call" and snapshot.get("call_id"):
            call = deepcopy(snapshot)
            call["_cached_at"] = cached_at
            task["calls"][str(snapshot["call_id"])] = call
        task["_cached_at"] = cached_at
        _write_state(state)


class WorkUpdates:
    """High-level durable update adapter for one contact manager."""

    def __init__(
        self,
        contact_id: str,
        *,
        client: InterfaceClient | None = None,
    ):
        self.contact_id = str(contact_id)
        self.client = client or InterfaceClient()
        contact = get_contact(self.contact_id)
        room_id = str((contact or {}).get("room_id") or "")
        if not room_id:
            raise WorkUpdateError(
                f"Contact '{self.contact_id}' has no Interface room."
            )
        self.contact = contact or {}
        self.room_id = room_id

    def _task_id(self, explicit: Any = "") -> str:
        task_id = str(explicit or active_task_id(self.contact_id) or "")
        if not task_id:
            raise WorkUpdateError(
                "No active durable task. Create one with task/create first."
            )
        return task_id

    def _refresh_task(self, task_id: str) -> dict[str, Any]:
        try:
            snapshot = _result_data(self.client.work_task_show(task_id))
        except Exception:
            return _task_cache(self.contact_id, task_id)
        _remember_task(self.contact_id, snapshot)
        return snapshot

    def _task_revision(self, task_id: str) -> int | None:
        cached = _task_cache(self.contact_id, task_id)
        value = cached.get("revision")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def execute(self, spec: dict[str, Any]) -> Any:
        action = str(spec.get("action") or spec.get("type") or "").strip().lower()
        data = spec.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise WorkUpdateError("work_update data must be an object.")
        payload = deepcopy(data)

        if action == "task/create":
            return self._task_create(payload)
        if action == "task/update":
            return self._task_update(spec, payload)
        if action == "todo/add":
            return self._todo_add(spec, payload)
        if action == "todo/update":
            return self._todo_update(spec, payload)
        if action == "milestone":
            return self._milestone(spec, payload)
        if action == "blocker/create":
            return self._blocker_create(spec, payload)
        if action == "blocker/resolve":
            return self._blocker_resolve(spec, payload)
        if action == "worker-group/create":
            return self._worker_group_create(spec, payload)
        if action == "worker-group/update":
            return self._worker_group_update(spec, payload)
        if action == "worker/create":
            return self._worker_create(spec, payload)
        if action == "worker/update":
            return self._worker_update(spec, payload)
        if action == "call/create":
            return self._call_create(spec, payload)
        if action == "call/update":
            return self._call_update(spec, payload)
        if action in TERMINAL_ACTIONS:
            return self._terminal(action, spec, payload)
        raise WorkUpdateError(f"Unknown work_update action '{action}'.")

    def _task_create(self, payload: dict[str, Any]) -> Any:
        task_id = str(payload.get("task_id") or _new_id("task", payload.get("title")))
        payload["task_id"] = task_id
        payload["room_id"] = self.room_id
        payload.setdefault("schema_version", 1)
        payload.setdefault("state", "running")
        todos = payload.get("todos")
        if isinstance(todos, list):
            for todo in todos:
                if isinstance(todo, dict):
                    todo.setdefault("todo_id", _new_id("todo", todo.get("title")))
        payload.setdefault("client_id", _stable_id("create-task", task_id))
        result = self.client.work_task_create(payload)
        snapshot = _result_data(result)
        _remember_task(self.contact_id, snapshot or payload)
        return result

    def _task_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id") or payload.pop("task_id", ""))
        if "revision" not in payload:
            revision = self._task_revision(task_id)
            if revision is not None:
                payload["revision"] = revision
        result = self.client.work_task_patch(task_id, payload)
        _remember_task(self.contact_id, _result_data(result))
        return result

    def _todo_add(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        todo_id = str(payload.get("todo_id") or _new_id("todo", payload.get("title")))
        payload["todo_id"] = todo_id
        payload.setdefault("client_id", _stable_id("create-todo", task_id, todo_id))
        result = self.client.work_todo_add(task_id, payload)
        _remember_task(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _todo_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        todo_id = str(spec.get("todo_id") or payload.pop("todo_id", ""))
        if not todo_id:
            raise WorkUpdateError("todo/update requires todo_id.")
        if "revision" not in payload:
            cached = _task_cache(self.contact_id, task_id)
            todo = (cached.get("todos") or {}).get(todo_id, {})
            revision = todo.get("revision") if isinstance(todo, dict) else None
            if isinstance(revision, int) and not isinstance(revision, bool):
                payload["revision"] = revision
        result = self.client.work_todo_patch(task_id, todo_id, payload)
        _remember_task(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _milestone(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        event_id = str(
            payload.get("work_event_id")
            or _new_id("milestone", payload.get("body"))
        )
        payload["work_event_id"] = event_id
        payload.setdefault("kind", "milestone")
        payload.setdefault("blocks", [])
        payload.setdefault("client_id", _stable_id("milestone", task_id, event_id))
        result = self.client.work_milestone_create(task_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _blocker_create(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        blocker_id = str(
            payload.get("blocker_id")
            or _new_id("blocker", payload.get("body"))
        )
        event_id = str(
            payload.get("work_event_id")
            or _stable_id("blocker-event", task_id, blocker_id)
        )
        payload.update(
            {
                "work_event_id": event_id,
                "blocker_id": blocker_id,
                "kind": "blocker",
            }
        )
        payload.setdefault("state", "open")
        payload.setdefault("resolved_at", None)
        payload.setdefault("blocks", [])
        payload.setdefault(
            "client_id",
            _stable_id("create-blocker", task_id, blocker_id),
        )
        result = self.client.work_blocker_create(task_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _blocker_resolve(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        blocker_id = str(spec.get("blocker_id") or payload.pop("blocker_id", ""))
        if not blocker_id:
            raise WorkUpdateError("blocker/resolve requires blocker_id.")
        if "revision" not in payload:
            cached = _task_cache(self.contact_id, task_id)
            for event in (cached.get("events") or {}).values():
                if (
                    isinstance(event, dict)
                    and event.get("kind") == "blocker"
                    and event.get("blocker_id") == blocker_id
                ):
                    revision = event.get("revision")
                    if isinstance(revision, int) and not isinstance(revision, bool):
                        payload["revision"] = revision
                    break
        payload.setdefault("state", "resolved")
        payload.setdefault("blocks", [])
        payload.setdefault(
            "client_id",
            _stable_id("resolve-blocker", task_id, blocker_id),
        )
        result = self.client.work_blocker_resolve(task_id, blocker_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_group_create(
        self,
        spec: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(
            payload.get("group_id")
            or _new_id("worker-group", payload.get("body"))
        )
        event_id = str(
            payload.get("work_event_id")
            or _stable_id("worker-group-event", task_id, group_id)
        )
        payload.update(
            {
                "group_id": group_id,
                "work_event_id": event_id,
                "kind": "worker_group",
            }
        )
        payload.setdefault("body", "Started workers")
        payload.setdefault("blocks", [])
        payload.setdefault("workers", [])
        payload.setdefault(
            "client_id",
            _stable_id("create-worker-group", task_id, group_id),
        )
        result = self.client.work_worker_group_create(task_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_group_update(
        self,
        spec: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(spec.get("group_id") or payload.pop("group_id", ""))
        if not group_id:
            raise WorkUpdateError("worker-group/update requires group_id.")
        if "revision" not in payload:
            cached = _task_cache(self.contact_id, task_id)
            group = (cached.get("worker_groups") or {}).get(group_id, {})
            revision = group.get("revision") if isinstance(group, dict) else None
            if isinstance(revision, int) and not isinstance(revision, bool):
                payload["revision"] = revision
        result = self.client.work_worker_group_patch(task_id, group_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_create(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(spec.get("group_id") or "")
        if not group_id:
            raise WorkUpdateError("worker/create requires group_id.")
        worker_id = str(payload.get("worker_id") or _new_id("worker"))
        invocation_id = str(
            payload.get("invocation_id")
            or _new_id("invocation", worker_id)
        )
        payload["worker_id"] = worker_id
        payload["invocation_id"] = invocation_id
        payload.setdefault("state", "in_progress")
        payload.setdefault("history", [])
        payload.setdefault(
            "client_id",
            _stable_id("create-worker", task_id, group_id, invocation_id),
        )
        result = self.client.work_worker_create(task_id, group_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(spec.get("group_id") or "")
        invocation_id = str(spec.get("invocation_id") or "")
        if not group_id or not invocation_id:
            raise WorkUpdateError(
                "worker/update requires group_id and invocation_id."
            )
        if payload.get("state") and payload["state"] not in WORKER_STATES:
            raise WorkUpdateError("worker/update state is invalid.")
        if "revision" not in payload:
            cached = _task_cache(self.contact_id, task_id)
            group = (cached.get("worker_groups") or {}).get(group_id, {})
            workers = group.get("workers") if isinstance(group, dict) else []
            for worker in workers or []:
                if (
                    isinstance(worker, dict)
                    and worker.get("invocation_id") == invocation_id
                ):
                    revision = worker.get("revision")
                    if isinstance(revision, int) and not isinstance(revision, bool):
                        payload["revision"] = revision
                    break
        result = self.client.work_worker_patch(
            task_id,
            group_id,
            invocation_id,
            payload,
        )
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _call_create(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        force_standalone = bool(spec.get("standalone"))
        task_id = (
            ""
            if force_standalone
            else str(spec.get("task_id") or active_task_id(self.contact_id) or "")
        )
        call_id = str(payload.get("call_id") or _new_id("call", payload.get("target_id")))
        event_id = str(
            payload.get("work_event_id")
            or _stable_id("call-event", task_id or self.room_id, call_id)
        )
        payload.update(
            {
                "call_id": call_id,
                "work_event_id": event_id,
                "kind": "call",
            }
        )
        payload.setdefault("state", "connecting")
        payload.setdefault("body", "")
        payload.setdefault("blocks", [])
        payload.setdefault("transcript", [])
        payload.setdefault(
            "client_id",
            _stable_id("create-call", task_id or self.room_id, call_id),
        )
        if task_id:
            result = self.client.work_call_create(task_id, payload)
        else:
            payload["room_id"] = self.room_id
            result = self.client.work_standalone_call_create(payload)
        _remember_event(self.contact_id, _result_data(result))
        if task_id:
            self._refresh_task(task_id)
        return result

    def _call_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        call_id = str(spec.get("call_id") or payload.pop("call_id", ""))
        if not call_id:
            raise WorkUpdateError("call/update requires call_id.")
        explicit_task_id = str(
            spec.get("task_id") or payload.pop("task_id", "") or ""
        )
        cached_standalone = _standalone_call_cache(self.contact_id, call_id)
        # Omitted task_id is the standalone route by contract. Never infer an
        # unrelated active task after local cache/correlation loss.
        task_id = "" if spec.get("standalone") else explicit_task_id
        if payload.get("state") and payload["state"] not in CALL_STATES:
            raise WorkUpdateError("call/update state is invalid.")
        reference = {
            "owner_contact_id": self.contact_id,
            "task_id": task_id,
            "call_id": call_id,
            "work_event_id": str(
                (
                    (
                        (_task_cache(self.contact_id, task_id).get("calls") or {})
                        .get(call_id, {})
                    )
                    if task_id
                    else cached_standalone
                ).get("work_event_id")
                or ""
            ),
        }
        if payload.get("state") in {"completed", "failed", "cancelled"}:
            _mark_call_correlations_terminal(call_id)
        retry_id = _journal_call_patch(reference, payload)
        result = _deliver_call_retry(retry_id, client=self.client)
        if isinstance(result, dict):
            return result
        return {
            "call_id": call_id,
            "task_id": task_id or None,
            "state": payload.get(
                "state",
                cached_standalone.get("state") or "in_progress",
            ),
            "queued_for_delivery": True,
        }

    def _terminal(
        self,
        action: str,
        spec: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        transition, kind, _ = TERMINAL_ACTIONS[action]
        event_id = str(
            payload.get("work_event_id")
            or _stable_id("terminal-event", task_id, transition)
        )
        payload["work_event_id"] = event_id
        payload.setdefault("kind", kind)
        payload.setdefault("body", "")
        payload.setdefault("blocks", [])
        payload.setdefault(
            "client_id",
            _stable_id(f"task-{transition}", task_id),
        )
        result = self.client.work_task_transition(task_id, transition, payload)
        _remember_event(self.contact_id, _result_data(result))
        snapshot = self._refresh_task(task_id)
        if not snapshot:
            with _state_guard():
                state = _read_state()
                contact = _contact_state(state, self.contact_id)
                task = contact["tasks"].setdefault(task_id, {})
                task["state"] = TERMINAL_ACTIONS[action][2]
                if contact.get("active_task_id") == task_id:
                    contact["active_task_id"] = ""
                _write_state(state)
        return result


def execute_work_update(
    tool_spec: dict[str, Any],
    contact_id: str,
    *,
    client: InterfaceClient | None = None,
) -> str:
    """Execute one manager work_update tool and return a manager-loop result."""
    action = str(tool_spec.get("action") or tool_spec.get("type") or "")
    try:
        result = WorkUpdates(contact_id, client=client).execute(tool_spec)
    except (WorkUpdateError, InterfaceError, OSError, ValueError) as exc:
        return f"Error: work_update {action or 'unknown'} failed: {exc}"
    except Exception as exc:
        return f"Error: work_update {action or 'unknown'} failed unexpectedly: {exc}"
    return f"Done. work_update {action}: {_public_result(result)}"


def refresh_task_snapshot(
    contact_id: str,
    task_id: str,
    *,
    client: InterfaceClient | None = None,
) -> dict[str, Any]:
    """Fetch Glass's current task and reconcile the local revision cache."""
    task_id = str(task_id or "")
    if not task_id:
        return {}
    try:
        snapshot = _result_data(
            (client or InterfaceClient()).work_task_show(task_id)
        )
    except Exception:
        return {}
    if not snapshot:
        return {}
    _remember_task(str(contact_id), snapshot)
    return deepcopy(snapshot)


def _clear_call_correlations(call_id: str) -> None:
    with _state_guard():
        state = _read_state()
        pending_call_ids = {
            str((entry.get("reference") or {}).get("call_id") or "")
            for entry in state.get("call_retry_journal", {}).values()
            if isinstance(entry, dict)
            and (
                entry.get("status", "pending") == "pending"
                or (
                    entry.get("status") == "dead_letter"
                    and entry.get("operation") == "create"
                )
            )
        }
        changed = False
        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if not isinstance(pending, dict):
                continue
            for peer_id, correlation in list(pending.items()):
                if not isinstance(correlation, dict):
                    continue
                if call_id in {
                    correlation.get("outbound_call_id"),
                    correlation.get("inbound_call_id"),
                }:
                    if not correlation.get("terminal_requested"):
                        continue
                    correlation_call_ids = {
                        str(correlation.get("outbound_call_id") or ""),
                        str(correlation.get("inbound_call_id") or ""),
                    }
                    if correlation_call_ids.intersection(pending_call_ids):
                        continue
                    pending.pop(peer_id, None)
                    changed = True
        if changed:
            _write_state(state)


def _mark_call_correlations_terminal(call_id: str) -> None:
    with _state_guard():
        state = _read_state()
        changed = False
        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if not isinstance(pending, dict):
                continue
            for correlation in pending.values():
                if not isinstance(correlation, dict):
                    continue
                if call_id in {
                    correlation.get("outbound_call_id"),
                    correlation.get("inbound_call_id"),
                }:
                    correlation["terminal_requested"] = True
                    changed = True
        if changed:
            _write_state(state)


def _touch_call_correlations(call_ids: set[str]) -> bool:
    """Atomically claim activity unless inactivity already closed the call."""
    call_ids = {str(value or "") for value in call_ids if value}
    if not call_ids:
        return False
    with _state_guard():
        state = _read_state()
        matches: list[dict[str, Any]] = []
        terminal_requested = False
        now = time.time()
        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if not isinstance(pending, dict):
                continue
            for correlation in pending.values():
                if not isinstance(correlation, dict):
                    continue
                if call_ids.intersection(
                    {
                        str(correlation.get("outbound_call_id") or ""),
                        str(correlation.get("inbound_call_id") or ""),
                    }
                ):
                    matches.append(correlation)
                    terminal_requested = bool(
                        terminal_requested
                        or correlation.get("terminal_requested")
                    )
        if not matches or terminal_requested:
            return False
        for correlation in matches:
            correlation["updated_at"] = now
        _write_state(state)
        return True


def _correlation_identity(correlation: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(correlation.get(field) or "")
        for field in (
            "outbound_owner_contact_id",
            "outbound_task_id",
            "outbound_call_id",
            "inbound_owner_contact_id",
            "inbound_task_id",
            "inbound_call_id",
        )
    )


def touch_manager_call_activity(
    contact_id: str,
    *,
    now: float | None = None,
) -> bool:
    """Refresh every live call owned by a manager that just did real work.

    Mirrored call cards share one inactivity boundary.  Updating every copy in
    the same state transaction prevents one side from expiring while the other
    side still appears active.  A terminal call is deliberately never revived.
    """
    contact_id = str(contact_id or "")
    if not contact_id:
        return False
    activity_at = time.time() if now is None else float(now)
    with _state_guard():
        state = _read_state()
        owner = state.get("contacts", {}).get(contact_id)
        if not isinstance(owner, dict):
            return False
        owner_pending = owner.get("pending_calls")
        if not isinstance(owner_pending, dict):
            return False
        identities = {
            _correlation_identity(correlation)
            for correlation in owner_pending.values()
            if isinstance(correlation, dict)
            and not correlation.get("terminal_requested")
            and any(_correlation_identity(correlation))
        }
        if not identities:
            return False

        grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {
            identity: [] for identity in identities
        }
        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if not isinstance(pending, dict):
                continue
            for correlation in pending.values():
                if not isinstance(correlation, dict):
                    continue
                identity = _correlation_identity(correlation)
                if identity in grouped:
                    grouped[identity].append(correlation)

        touched = False
        for correlations in grouped.values():
            if not correlations or any(
                correlation.get("terminal_requested")
                for correlation in correlations
            ):
                continue
            for correlation in correlations:
                correlation["updated_at"] = activity_at
            touched = True
        if touched:
            _write_state(state)
        return touched


def _correlation_references(
    correlation: dict[str, Any],
) -> list[tuple[str, dict[str, str]]]:
    references: list[tuple[str, dict[str, str]]] = []
    for side in ("outbound", "inbound"):
        owner = str(correlation.get(f"{side}_owner_contact_id") or "")
        call_id = str(correlation.get(f"{side}_call_id") or "")
        if not owner or not call_id:
            continue
        references.append(
            (
                side,
                {
                    "owner_contact_id": owner,
                    "task_id": str(
                        correlation.get(f"{side}_task_id") or ""
                    ),
                    "call_id": call_id,
                    "work_event_id": str(
                        correlation.get(f"{side}_work_event_id") or ""
                    ),
                },
            )
        )
    return references


def _call_reference_identity(reference: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(reference.get("owner_contact_id") or ""),
        str(reference.get("task_id") or ""),
        str(reference.get("call_id") or ""),
    )


def complete_inactive_calls(
    *,
    now: float | None = None,
    client: InterfaceClient | None = None,
) -> int:
    """Complete calls after ten seconds without correlated manager activity.

    Completion and its retry journal entries are committed in one state-file
    transaction. Once claimed, the old correlation is terminal and future
    activity creates a new call rather than reviving the completed card.
    """
    now = time.time() if now is None else float(now)
    cutoff = now - CALL_IDLE_TIMEOUT_SECONDS
    retry_ids: list[str] = []
    completed_count = 0

    with _state_guard():
        state = _read_state()
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        correlated_references: set[tuple[str, str, str]] = set()

        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if not isinstance(pending, dict):
                continue
            for correlation in pending.values():
                if not isinstance(correlation, dict):
                    continue
                identity = _correlation_identity(correlation)
                references = _correlation_references(correlation)
                if not any(identity) or not references:
                    continue
                for _side, reference in references:
                    correlated_references.add(
                        _call_reference_identity(reference)
                    )
                candidate = grouped.setdefault(
                    identity,
                    {
                        "correlation": deepcopy(correlation),
                        "updated_at": 0.0,
                        "terminal_requested": False,
                        "instances": [],
                    },
                )
                candidate["updated_at"] = max(
                    float(candidate.get("updated_at") or 0.0),
                    _timestamp(correlation.get("updated_at")),
                )
                candidate["terminal_requested"] = bool(
                    candidate.get("terminal_requested")
                    or correlation.get("terminal_requested")
                )
                candidate["instances"].append(correlation)

        entries: list[dict[str, Any]] = []
        terminal_correlations: list[dict[str, Any]] = []
        entry_identities: set[tuple[str, str, str]] = set()
        for candidate in grouped.values():
            updated_at = float(candidate.get("updated_at") or 0.0)
            if (
                candidate.get("terminal_requested")
                or not updated_at
                or updated_at > cutoff
            ):
                continue
            correlation = dict(candidate.get("correlation") or {})
            added = False
            for side, reference in _correlation_references(correlation):
                reference_identity = _call_reference_identity(reference)
                if reference_identity in entry_identities:
                    continue
                entries.append(
                    _call_patch_entry(
                        reference,
                        {"state": "completed"},
                        mutation_id="idle-complete",
                        direction=side,
                    )
                )
                entry_identities.add(reference_identity)
                added = True
            if added:
                terminal_correlations.extend(candidate["instances"])
                completed_count += 1

        # Recover an already-visible card whose live correlation was lost or
        # overwritten. Its per-card cache timestamp is the last accepted call
        # mutation, so it follows the same inactivity rule.
        orphan_calls: list[dict[str, Any]] = []
        for owner, contact in state.get("contacts", {}).items():
            if not isinstance(contact, dict):
                continue
            for call_id, call in (
                contact.get("standalone_calls", {}) or {}
            ).items():
                if not isinstance(call, dict):
                    continue
                reference = {
                    "owner_contact_id": str(owner),
                    "task_id": "",
                    "call_id": str(call_id),
                    "work_event_id": str(call.get("work_event_id") or ""),
                }
                identity = _call_reference_identity(reference)
                if (
                    identity in correlated_references
                    or identity in entry_identities
                    or call.get("_idle_terminal_requested")
                    or call.get("state") not in {"connecting", "in_progress"}
                    or not _timestamp(call.get("_cached_at"))
                    or _timestamp(call.get("_cached_at")) > cutoff
                ):
                    continue
                entries.append(
                    _call_patch_entry(
                        reference,
                        {"state": "completed"},
                        mutation_id="idle-complete",
                        direction=str(call.get("direction") or "mutation"),
                    )
                )
                entry_identities.add(identity)
                orphan_calls.append(call)
                completed_count += 1

            for task_id, task in (contact.get("tasks", {}) or {}).items():
                if not isinstance(task, dict):
                    continue
                task_cached_at = _timestamp(task.get("_cached_at"))
                for call_id, call in (task.get("calls", {}) or {}).items():
                    if not isinstance(call, dict):
                        continue
                    reference = {
                        "owner_contact_id": str(owner),
                        "task_id": str(task_id),
                        "call_id": str(call_id),
                        "work_event_id": str(
                            call.get("work_event_id") or ""
                        ),
                    }
                    identity = _call_reference_identity(reference)
                    cached_at = _timestamp(
                        call.get("_cached_at") or task_cached_at
                    )
                    if (
                        identity in correlated_references
                        or identity in entry_identities
                        or call.get("_idle_terminal_requested")
                        or call.get("state")
                        not in {"connecting", "in_progress"}
                        or not cached_at
                        or cached_at > cutoff
                    ):
                        continue
                    entries.append(
                        _call_patch_entry(
                            reference,
                            {"state": "completed"},
                            mutation_id="idle-complete",
                            direction=str(
                                call.get("direction") or "mutation"
                            ),
                        )
                    )
                    entry_identities.add(identity)
                    orphan_calls.append(call)
                    completed_count += 1

        if not entries:
            return 0
        try:
            retry_ids = _insert_call_retry_entries_in_state(state, entries)
        except Exception:
            _write_state(state)
            raise
        for correlation in terminal_correlations:
            correlation["terminal_requested"] = True
            correlation["terminal_requested_at"] = now
            correlation["terminal_reason"] = "inactivity"
        for call in orphan_calls:
            call["_idle_terminal_requested"] = True
        _write_state(state)

    for retry_id in retry_ids:
        _schedule_call_retry(retry_id, client=client)
    return completed_count


def _discard_call_reference(call_id: str, prefix: str) -> None:
    """Drop one failed remote card while preserving its valid peer card."""
    prefix = "inbound" if prefix == "inbound" else "outbound"
    with _state_guard():
        state = _read_state()
        changed = False
        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if not isinstance(pending, dict):
                continue
            for peer_id, correlation in list(pending.items()):
                if (
                    not isinstance(correlation, dict)
                    or correlation.get(f"{prefix}_call_id") != call_id
                ):
                    continue
                for field in (
                    "owner_contact_id",
                    "task_id",
                    "call_id",
                    "work_event_id",
                ):
                    correlation[f"{prefix}_{field}"] = ""
                correlation["updated_at"] = time.time()
                if not (
                    _has_call_reference(correlation, "outbound")
                    or _has_call_reference(correlation, "inbound")
                ):
                    pending.pop(peer_id, None)
                changed = True
        if changed:
            _write_state(state)


def _remember_outbound_call_reference(reference: dict[str, Any]) -> None:
    """Durably retain the local side once primary message delivery is accepted."""
    contact_id = str(reference.get("owner_contact_id") or "")
    target_id = str(reference.get("target_id") or "")
    call_id = str(reference.get("call_id") or "")
    if (
        reference.get("continuation")
        or not contact_id
        or not target_id
        or not call_id
    ):
        return
    correlation = {
        "outbound_owner_contact_id": contact_id,
        "outbound_task_id": str(reference.get("task_id") or ""),
        "outbound_call_id": call_id,
        "outbound_work_event_id": str(reference.get("work_event_id") or ""),
        "inbound_owner_contact_id": "",
        "inbound_task_id": "",
        "inbound_call_id": "",
        "inbound_work_event_id": "",
        "source_kind": str(reference.get("target_kind") or ""),
        "source_id": target_id,
        "updated_at": time.time(),
    }
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        contact["pending_calls"][target_id] = correlation
        _write_state(state)


def record_worker_started(
    contact_id: str,
    worker_id: str,
    worker_type: str,
    description: str,
    *,
    queued: bool = False,
    task_id: str = "",
    invocation_id: str = "",
    state_name: str = "",
    state_description: str = "",
    client: InterfaceClient | None = None,
) -> dict[str, str]:
    """Best-effort bridge from the real worker lifecycle to a durable card."""
    task_id = str(task_id or active_task_id(contact_id) or "")
    if not task_id:
        return {}
    group_id = _stable_id("worker-group", contact_id, task_id)
    invocation_id = str(invocation_id or _new_id("invocation", worker_id))
    worker_state = str(
        state_name or ("yet_to_start" if queued else "in_progress")
    )
    if worker_state not in WORKER_STATES:
        worker_state = "yet_to_start" if queued else "in_progress"
    try:
        updates = WorkUpdates(contact_id, client=client)
    except Exception:
        return {}
    cached = _task_cache(contact_id, task_id)
    if group_id not in (cached.get("worker_groups") or {}):
        try:
            updates.execute(
                {
                    "action": "worker-group/create",
                    "task_id": task_id,
                    "data": {
                        "group_id": group_id,
                        "work_event_id": _stable_id(
                            "worker-group-event",
                            contact_id,
                            task_id,
                        ),
                        "body": "Started workers",
                    },
                }
            )
        except Exception:
            return {}
    try:
        updates.execute(
            {
                "action": "worker/create",
                "task_id": task_id,
                "group_id": group_id,
                "data": {
                    "worker_id": worker_id,
                    "invocation_id": invocation_id,
                    "name": _compact(description, 500) or worker_id,
                    "description": (
                        _compact(state_description, 500)
                        or (
                            "Queued and waiting to launch"
                            if queued
                            else f"{worker_type.capitalize()} worker is running"
                        )
                    ),
                    "state": worker_state,
                    "history": [],
                },
            }
        )
    except Exception:
        return {}
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        contact["workers"][worker_id] = {
            "task_id": task_id,
            "group_id": group_id,
            "invocation_id": invocation_id,
            "state": worker_state,
        }
        _write_state(state)
    return {
        "task_id": task_id,
        "group_id": group_id,
        "invocation_id": invocation_id,
    }


def record_worker_state(
    contact_id: str,
    worker_id: str,
    state_name: str,
    description: str = "",
    *,
    client: InterfaceClient | None = None,
) -> bool:
    """Revise the exact invocation correlated to a real worker run."""
    if state_name not in WORKER_STATES:
        return False
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        correlation = deepcopy(contact.get("workers", {}).get(worker_id) or {})
    if not correlation:
        try:
            from core.long_task_updates import record_pending_worker_state

            return record_pending_worker_state(
                contact_id,
                worker_id,
                state_name,
                description,
            )
        except Exception:
            return False
    try:
        WorkUpdates(contact_id, client=client).execute(
            {
                "action": "worker/update",
                "task_id": correlation["task_id"],
                "group_id": correlation["group_id"],
                "invocation_id": correlation["invocation_id"],
                "data": {
                    "state": state_name,
                    "description": _compact(description, 500),
                },
            }
        )
    except Exception:
        try:
            from core.long_task_updates import record_pending_worker_state

            return record_pending_worker_state(
                contact_id,
                worker_id,
                state_name,
                description,
            )
        except Exception:
            return False
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        current = contact.get("workers", {}).get(worker_id)
        if isinstance(current, dict):
            if state_name in {"completed", "failed", "cancelled"}:
                contact.get("workers", {}).pop(worker_id, None)
            else:
                current["state"] = state_name
            _write_state(state)
    return True


def prepare_outbound_call(
    contact_id: str,
    *,
    target_kind: str,
    target_id: str,
    target_name: str,
    message: str,
    task_id: str = "",
) -> dict[str, Any]:
    """Allocate local correlation without performing any network operation."""
    task_id = str(task_id or active_task_id(contact_id) or "")
    target_kind = "silicon" if target_kind == "silicon" else "manager"
    with _state_guard():
        state = _read_state()
        contact_state = _contact_state(state, contact_id)
        pending = deepcopy(
            contact_state.get("pending_calls", {}).get(str(target_id)) or {}
        )
    if not pending.get("terminal_requested") and (
        _has_call_reference(pending, "outbound")
        or _has_call_reference(pending, "inbound")
    ):
        local_reference = _call_reference_for_owner(pending, contact_id)
        return {
            **local_reference,
            "target_kind": target_kind,
            "target_id": target_id,
            "continuation": True,
        }
    call_id = _new_id("call", target_id)
    return {
        "owner_contact_id": contact_id,
        "task_id": task_id,
        "call_id": call_id,
        "work_event_id": _stable_id(
            "call-event",
            contact_id,
            task_id,
            call_id,
        ),
        "target_kind": target_kind,
        "target_id": target_id,
    }


def _refresh_stale_call_continuation(
    reference: dict[str, Any],
    *,
    target_name: str,
    message: str,
) -> bool:
    """Move a prepared late continuation onto a current or fresh call."""
    if not reference.get("continuation"):
        return False
    owner = str(reference.get("owner_contact_id") or "")
    target_id = str(reference.get("target_id") or "")
    with _state_guard():
        state = _read_state()
        correlation = deepcopy(
            _contact_state(state, owner)
            .get("pending_calls", {})
            .get(target_id)
            or {}
        )
    current_reference = _call_reference_for_owner(correlation, owner)
    if (
        correlation
        and not correlation.get("terminal_requested")
        and str(current_reference.get("call_id") or "")
        == str(reference.get("call_id") or "")
    ):
        return False
    fresh = prepare_outbound_call(
        owner,
        target_kind=str(reference.get("target_kind") or "manager"),
        target_id=target_id,
        target_name=target_name,
        message=message,
        task_id=str(reference.get("task_id") or ""),
    )
    reference.clear()
    reference.update(fresh)
    return True


def enqueue_outbound_call(
    reference: dict[str, Any],
    *,
    target_name: str,
    message: str,
    client: InterfaceClient | None = None,
    idempotency_key: str = "",
) -> bool:
    """Persist a prepared call card after primary delivery has been accepted."""
    if (
        reference.get("continuation")
        and _call_retry_dedupe_receipt(idempotency_key).get("kind") == "append"
    ):
        # Reconciliation can replay an already-accepted continuation after its
        # terminal delivery removed the live correlation.  A receipt proves
        # that exact delivery succeeded; do not reinterpret the replay as new
        # future activity and leave behind a phantom pending call.
        return True
    _refresh_stale_call_continuation(
        reference,
        target_name=target_name,
        message=message,
    )
    if not reference.get("continuation"):
        retry_id = _journal_call_create(
            "outbound",
            reference,
            target_name=target_name,
            message=message,
            dedupe_key=idempotency_key,
        )
        canonical = (
            _call_retry_dedupe_result(idempotency_key, retry_id)
            if idempotency_key
            else deepcopy(reference)
        )
        _remember_outbound_call_reference(canonical or reference)
        _schedule_call_retry(retry_id, client=client)
        return True
    owner = str(reference.get("owner_contact_id") or "")
    target_id = str(reference.get("target_id") or "")
    with _state_guard():
        state = _read_state()
        correlation = deepcopy(
            _contact_state(state, owner)
            .get("pending_calls", {})
            .get(target_id)
            or {}
        )
    if not correlation:
        return False
    role = str(
        reference.get("continuation_role")
        or _call_role_for_owner(correlation, owner)
    )
    return _append_correlated_call(
        correlation,
        speaker_kind="manager",
        speaker_id=f"manager:{owner}",
        speaker_name=str(get_own_profile().get("name") or "Silicon manager"),
        message=message,
        client=client,
        dedupe_key=idempotency_key,
        terminal=role == "inbound",
    )


def record_outbound_call(
    contact_id: str,
    *,
    target_kind: str,
    target_id: str,
    target_name: str,
    message: str,
    task_id: str = "",
    client: InterfaceClient | None = None,
) -> dict[str, Any]:
    """Synchronously create a call card for explicit work-update callers."""
    reference = prepare_outbound_call(
        contact_id,
        target_kind=target_kind,
        target_id=target_id,
        target_name=target_name,
        message=message,
        task_id=task_id,
    )
    _refresh_stale_call_continuation(
        reference,
        target_name=target_name,
        message=message,
    )
    if not reference.get("continuation"):
        retry_id = _journal_call_create(
            "outbound",
            reference,
            target_name=target_name,
            message=message,
        )
        _remember_outbound_call_reference(reference)
        _deliver_call_retry(retry_id, client=client)
        return reference
    owner = str(reference.get("owner_contact_id") or "")
    target_id = str(reference.get("target_id") or "")
    with _state_guard():
        state = _read_state()
        correlation = deepcopy(
            _contact_state(state, owner)
            .get("pending_calls", {})
            .get(target_id)
            or {}
        )
    role = str(
        reference.get("continuation_role")
        or _call_role_for_owner(correlation, owner)
    )
    if not correlation or not _append_correlated_call(
        correlation,
        speaker_kind="manager",
        speaker_id=f"manager:{owner}",
        speaker_name=str(get_own_profile().get("name") or "Silicon manager"),
        message=message,
        client=client,
        synchronous=True,
        terminal=role == "inbound",
    ):
        return {}
    return reference


def _append_correlated_call(
    correlation: dict[str, Any],
    *,
    speaker_kind: str,
    speaker_id: str,
    speaker_name: str,
    message: str,
    client: InterfaceClient | None = None,
    synchronous: bool = False,
    dedupe_key: str = "",
    terminal: bool = False,
) -> bool:
    """Journal each mirrored side independently before any network attempt."""
    call_ids = {
        str(correlation.get("outbound_call_id") or ""),
        str(correlation.get("inbound_call_id") or ""),
    }
    if not _touch_call_correlations(call_ids):
        return False
    message_id = (
        _stable_id("call-message", dedupe_key)
        if dedupe_key
        else _new_id("call-message")
    )
    occurred_at = _utc_now()
    entries: list[dict[str, Any]] = []
    for side in ("outbound", "inbound"):
        owner = str(correlation.get(f"{side}_owner_contact_id") or "")
        call_id = str(correlation.get(f"{side}_call_id") or "")
        if not owner or not call_id:
            continue
        reference = {
            "owner_contact_id": owner,
            "task_id": str(correlation.get(f"{side}_task_id") or ""),
            "call_id": call_id,
            "work_event_id": str(
                correlation.get(f"{side}_work_event_id") or ""
            ),
        }
        transcript = {
            "transcript_id": _stable_id(
                "transcript-message",
                message_id,
                side,
            ),
            "speaker_kind": (
                "silicon" if speaker_kind == "silicon" else "manager"
            ),
            "speaker_id": _safe_fragment(speaker_id, "speaker"),
            "speaker_name": speaker_name or speaker_id,
            "body": message,
            "blocks": [],
            "revision": 0,
            "created_at": occurred_at,
            "updated_at": occurred_at,
        }
        entries.append(
            _call_patch_entry(
                reference,
                {
                    "state": "completed" if terminal else "in_progress",
                    "transcript": [transcript],
                },
                mutation_id=f"{message_id}:{side}",
                direction=side,
                occurred_at=occurred_at,
            )
        )
    if not entries:
        return False
    receipt = _call_retry_dedupe_receipt(dedupe_key)
    if receipt.get("kind") == "create":
        # The durable self-echo of an accepted initial message reuses the
        # create's idempotency key. Re-drive that create, but do not append the
        # same message or close a call that has not received a response.
        retry_ids = [
            str(retry_id)
            for retry_id in receipt.get("retry_ids") or []
            if retry_id
        ]
        for retry_id in retry_ids:
            if synchronous:
                _deliver_call_retry(retry_id, client=client)
            else:
                _schedule_call_retry(retry_id, client=client)
        return True
    retry_ids = _insert_call_retry_entries(
        entries,
        dedupe_key=dedupe_key,
        dedupe_kind="append",
    )
    if terminal:
        for entry in entries:
            _mark_call_correlations_terminal(
                str((entry.get("reference") or {}).get("call_id") or "")
            )
    for retry_id in retry_ids:
        if synchronous:
            _deliver_call_retry(retry_id, client=client)
        else:
            _schedule_call_retry(retry_id, client=client)
    # Once durable, this is accepted even if either remote side is unavailable.
    return True


def prepare_inbound_call(
    contact_id: str,
    *,
    source_kind: str,
    source_id: str,
    source_name: str,
    message: str,
    outbound: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Allocate an inbound reference without publishing or caching it."""
    outbound = dict(outbound or {})
    task_id = active_task_id(contact_id)
    call_id = _new_id("call", source_id)
    work_event_id = _stable_id(
        "call-event",
        contact_id,
        task_id,
        call_id,
    )
    return {
        "owner_contact_id": contact_id,
        "task_id": task_id,
        "call_id": call_id,
        "work_event_id": work_event_id,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_name": source_name,
        "message": message,
        "outbound_owner_contact_id": str(
            outbound.get("owner_contact_id")
            or outbound.get("outbound_owner_contact_id")
            or ""
        ),
        "outbound_task_id": str(
            outbound.get("task_id") or outbound.get("outbound_task_id") or ""
        ),
        "outbound_call_id": str(
            outbound.get("call_id") or outbound.get("outbound_call_id") or ""
        ),
        "outbound_work_event_id": str(
            outbound.get("work_event_id")
            or outbound.get("outbound_work_event_id")
            or ""
        ),
        "inbound_owner_contact_id": contact_id,
        "inbound_task_id": task_id,
        "inbound_call_id": call_id,
        "inbound_work_event_id": work_event_id,
    }


def _remember_inbound_call_reference(reference: dict[str, Any]) -> None:
    """Publish the canonical inbound/outbound correlation after journaling."""
    contact_id = str(reference.get("owner_contact_id") or "")
    source_id = str(reference.get("source_id") or "")
    call_id = str(reference.get("call_id") or "")
    if not contact_id or not source_id or not call_id:
        return
    correlation = {
        "outbound_owner_contact_id": str(
            reference.get("outbound_owner_contact_id") or ""
        ),
        "outbound_task_id": str(reference.get("outbound_task_id") or ""),
        "outbound_call_id": str(reference.get("outbound_call_id") or ""),
        "outbound_work_event_id": str(
            reference.get("outbound_work_event_id") or ""
        ),
        "inbound_owner_contact_id": contact_id,
        "inbound_task_id": str(reference.get("task_id") or ""),
        "inbound_call_id": call_id,
        "inbound_work_event_id": str(reference.get("work_event_id") or ""),
        "source_kind": str(reference.get("source_kind") or ""),
        "source_id": source_id,
        "updated_at": time.time(),
    }
    with _state_guard():
        state = _read_state()
        recipient = _contact_state(state, contact_id)
        recipient["pending_calls"][source_id] = deepcopy(correlation)
        outbound_owner = correlation["outbound_owner_contact_id"]
        if outbound_owner:
            owner = _contact_state(state, outbound_owner)
            owner["pending_calls"][contact_id] = deepcopy(correlation)
        _write_state(state)


def _claim_call_retry(
    retry_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Claim the oldest mutation in a call lane across rolling processes."""
    now = time.time() if now is None else float(now)
    owner = _call_retry_owner()
    with _state_guard():
        state = _read_state()
        # Persist retention pruning even if this entry is no longer claimable.
        _write_state(state)
        journal = state.get("call_retry_journal", {})
        entry = journal.get(retry_id)
        if not isinstance(entry, dict) or entry.get("status") != "pending":
            return {}
        if float(entry.get("next_attempt_at") or 0.0) > now:
            return {}
        lane = str(entry.get("lane") or _call_retry_lane(entry["reference"]))
        lane_entries = [
            candidate
            for candidate in journal.values()
            if isinstance(candidate, dict)
            and str(
                candidate.get("lane")
                or _call_retry_lane(candidate.get("reference") or {})
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
                "lease_expires_at": now + CALL_RETRY_LEASE_SECONDS,
            }
        )
        _write_state(state)
        return deepcopy(entry)


def _cached_call_snapshot(reference: dict[str, Any]) -> dict[str, Any]:
    owner = str(reference.get("owner_contact_id") or "")
    task_id = str(reference.get("task_id") or "")
    call_id = str(reference.get("call_id") or "")
    if task_id:
        calls = _task_cache(owner, task_id).get("calls") or {}
        value = calls.get(call_id) if isinstance(calls, dict) else {}
        return deepcopy(value) if isinstance(value, dict) else {}
    return _standalone_call_cache(owner, call_id)


def _call_mutation_satisfied(
    snapshot: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    if not snapshot:
        return False
    for field in ("state", "body", "blocks"):
        if field in payload and snapshot.get(field) != payload[field]:
            return False
    desired_transcript = payload.get("transcript")
    if desired_transcript:
        current = {
            str(row.get("transcript_id") or ""): row
            for row in snapshot.get("transcript") or []
            if isinstance(row, dict)
        }
        for desired in desired_transcript:
            if not isinstance(desired, dict):
                return False
            existing = current.get(str(desired.get("transcript_id") or ""))
            if not isinstance(existing, dict):
                return False
            if any(existing.get(key) != value for key, value in desired.items()):
                return False
    return True


def _deliver_call_patch(
    transport: InterfaceClient,
    reference: dict[str, Any],
    payload: dict[str, Any],
) -> Any:
    """Resolve optimistic revision at attempt time and reconcile replays."""
    task_id = str(reference.get("task_id") or "")
    call_id = str(reference.get("call_id") or "")
    snapshot = _cached_call_snapshot(reference)
    if _call_mutation_satisfied(snapshot, payload):
        return snapshot
    attempt_payload = deepcopy(payload)
    revision = snapshot.get("revision") if isinstance(snapshot, dict) else None
    if isinstance(revision, int) and not isinstance(revision, bool):
        attempt_payload["revision"] = revision

    def patch() -> Any:
        if task_id:
            return transport.work_call_patch(
                task_id,
                call_id,
                attempt_payload,
            )
        return transport.work_standalone_call_patch(call_id, attempt_payload)

    try:
        return patch()
    except WorkCallMutationError as exc:
        if exc.status_code != 409 or exc.current_revision is None:
            raise
        # A lost success leaves the cached revision stale. Re-apply the exact
        # semantic mutation at the authoritative revision; stable transcript
        # ids/timestamps make this content-idempotent.
        attempt_payload["revision"] = exc.current_revision
        return patch()


def _deliver_call_retry(
    retry_id: str,
    *,
    client: InterfaceClient | None = None,
    claim_now: float | None = None,
) -> Any:
    """Attempt one claimed call mutation and retain it on any failure."""
    entry = _claim_call_retry(retry_id, now=claim_now)
    if not entry:
        with _CALL_RETRY_LOCK:
            _CALL_RETRY_INFLIGHT.discard(retry_id)
        return False
    reference = dict(entry.get("reference") or {})
    task_id = str(reference.get("task_id") or "")
    call_id = str(reference.get("call_id") or "")
    payload = deepcopy(entry.get("payload") or {})
    try:
        if not call_id or not payload:
            raise WorkUpdateError("Journaled call mutation is incomplete.")
        transport = client or InterfaceClient()
        if entry.get("operation") == "create" and task_id:
            result = transport.work_call_create(task_id, payload)
        elif entry.get("operation") == "create":
            result = transport.work_standalone_call_create(payload)
        elif entry.get("operation") == "patch":
            result = _deliver_call_patch(transport, reference, payload)
        else:
            raise WorkUpdateError("Journaled call mutation has no operation.")
        snapshot = _result_data(result)
        if snapshot:
            _remember_event(
                str(reference.get("owner_contact_id") or ""),
                snapshot,
            )
    except Exception as exc:
        _record_call_retry_failure(retry_id, entry, exc)
        failed = _call_retry_entry(retry_id)
        if (
            failed.get("status") == "dead_letter"
            and failed.get("operation") != "create"
        ):
            _schedule_next_call_lane(failed, client=client)
        return False
    else:
        removed = _complete_call_retry(retry_id, entry)
        if removed:
            _clear_call_correlations(call_id)
            _schedule_next_call_lane(entry, client=client)
        return result
    finally:
        with _CALL_RETRY_LOCK:
            _CALL_RETRY_INFLIGHT.discard(retry_id)


def _schedule_call_retry(
    retry_id: str,
    *,
    client: InterfaceClient | None = None,
    claim_now: float | None = None,
) -> bool:
    with _CALL_RETRY_LOCK:
        if retry_id in _CALL_RETRY_INFLIGHT:
            return True
        _CALL_RETRY_INFLIGHT.add(retry_id)
    entry = _call_retry_entry(retry_id)
    lane = str(
        entry.get("lane")
        or _call_retry_lane(entry.get("reference") or {})
    )
    accepted = submit_best_effort(
        _deliver_call_retry,
        retry_id,
        client=client,
        claim_now=claim_now,
        key=f"work-call-retry:{lane}",
    )
    if not accepted:
        with _CALL_RETRY_LOCK:
            _CALL_RETRY_INFLIGHT.discard(retry_id)
    return accepted


def _schedule_next_call_lane(
    completed: dict[str, Any],
    *,
    client: InterfaceClient | None = None,
) -> bool:
    lane = str(
        completed.get("lane")
        or _call_retry_lane(completed.get("reference") or {})
    )
    now = time.time()
    with _state_guard():
        state = _read_state()
        candidates = sorted(
            (
                entry
                for entry in state.get("call_retry_journal", {}).values()
                if isinstance(entry, dict)
                and str(
                    entry.get("lane")
                    or _call_retry_lane(entry.get("reference") or {})
                )
                == lane
                and (
                    entry.get("status", "pending") == "pending"
                    or (
                        entry.get("status") == "dead_letter"
                        and entry.get("operation") == "create"
                    )
                )
            ),
            key=lambda entry: (
                int(entry.get("sequence") or 0),
                float(entry.get("created_at") or 0.0),
                str(entry.get("retry_id") or ""),
            ),
        )
    if not candidates:
        return False
    next_entry = candidates[0]
    if (
        next_entry.get("status", "pending") != "pending"
        or float(next_entry.get("next_attempt_at") or 0.0) > now
    ):
        return False
    return _schedule_call_retry(
        str(next_entry.get("retry_id") or ""),
        client=client,
    )


def replay_pending_call_updates(
    *,
    limit: int = CALL_RETRY_BATCH_LIMIT,
    now: float | None = None,
    client: InterfaceClient | None = None,
) -> int:
    """Schedule due disk-journaled call cards after startup or a periodic tick."""
    now = time.time() if now is None else float(now)
    with _state_guard():
        state = _read_state()
        _write_state(state)
        journal = state.get("call_retry_journal", {})
        ordered = sorted(
            (entry for entry in journal.values() if isinstance(entry, dict)),
            key=lambda entry: (
                int(entry.get("sequence") or 0),
                float(entry.get("created_at") or 0.0),
                str(entry.get("retry_id") or ""),
            ),
        )
        first_by_lane: dict[str, dict[str, Any]] = {}
        for entry in ordered:
            if not (
                entry.get("status", "pending") == "pending"
                or (
                    entry.get("status") == "dead_letter"
                    and entry.get("operation") == "create"
                )
            ):
                continue
            lane = str(
                entry.get("lane")
                or _call_retry_lane(entry.get("reference") or {})
            )
            first_by_lane.setdefault(lane, entry)
        eligible = [
            entry
            for entry in first_by_lane.values()
            if entry.get("status", "pending") == "pending"
            and float(entry.get("next_attempt_at") or 0.0) <= now
            and float(entry.get("lease_expires_at") or 0.0) <= now
        ][: max(0, int(limit))]
    scheduled = 0
    for entry in eligible:
        retry_id = str(entry.get("retry_id") or "")
        if retry_id and _schedule_call_retry(
            retry_id,
            client=client,
            claim_now=now,
        ):
            scheduled += 1
    return scheduled


def pending_call_update_retries(
    *,
    persist_prune: bool = True,
) -> dict[str, Any]:
    """Return body-free retry health for diagnostics and runtime probes."""
    with _state_guard():
        state = _read_state()
        if persist_prune:
            _write_state(state)
        journal = state.get("call_retry_journal", {})
        entries = [entry for entry in journal.values() if isinstance(entry, dict)]
        archived = state.get("call_retry_dead_letters")
        archived_count = len(archived) if isinstance(archived, list) else 0
        overflow_count = int(state.get("call_retry_overflow_count") or 0)
        last_overflow_at = float(
            state.get("call_retry_last_overflow_at") or 0.0
        )
    pending = [
        entry for entry in entries if entry.get("status", "pending") == "pending"
    ]
    dead_letter = [
        entry for entry in entries if entry.get("status") == "dead_letter"
    ]
    return {
        "pending": len(pending),
        "failed": sum(
            1 for entry in pending if int(entry.get("attempts") or 0) > 0
        ),
        "dead_letter": len(dead_letter),
        "total": len(entries),
        "archived_dead_letter": archived_count,
        "overflow_count": overflow_count,
        "last_overflow_at": last_overflow_at,
        "oldest_created_at": min(
            (float(entry.get("created_at") or 0.0) for entry in entries),
            default=0.0,
        ),
        "next_attempt_at": min(
            (float(entry.get("next_attempt_at") or 0.0) for entry in entries),
            default=0.0,
        ),
    }


def enqueue_inbound_call(
    contact_id: str,
    *,
    source_kind: str,
    source_id: str,
    source_name: str,
    message: str,
    outbound: dict[str, Any] | None = None,
    client: InterfaceClient | None = None,
    idempotency_key: str = "",
) -> dict[str, str]:
    reference = prepare_inbound_call(
        contact_id,
        source_kind=source_kind,
        source_id=source_id,
        source_name=source_name,
        message=message,
        outbound=outbound,
    )
    retry_id = _journal_call_create(
        "inbound",
        reference,
        dedupe_key=idempotency_key,
    )
    canonical = (
        _call_retry_dedupe_result(idempotency_key, retry_id)
        if idempotency_key
        else deepcopy(reference)
    )
    reference = canonical or reference
    _remember_inbound_call_reference(reference)
    _schedule_call_retry(retry_id, client=client)
    return {
        "owner_contact_id": str(reference.get("owner_contact_id") or ""),
        "task_id": str(reference.get("task_id") or ""),
        "call_id": str(reference.get("call_id") or ""),
        "work_event_id": str(reference.get("work_event_id") or ""),
    }


def record_inbound_call(
    contact_id: str,
    *,
    source_kind: str,
    source_id: str,
    source_name: str,
    message: str,
    outbound: dict[str, Any] | None = None,
    client: InterfaceClient | None = None,
    idempotency_key: str = "",
) -> dict[str, str]:
    """Synchronously create an inbound call for explicit work-update callers."""
    reference = prepare_inbound_call(
        contact_id,
        source_kind=source_kind,
        source_id=source_id,
        source_name=source_name,
        message=message,
        outbound=outbound,
    )
    retry_id = _journal_call_create(
        "inbound",
        reference,
        dedupe_key=idempotency_key,
    )
    canonical = (
        _call_retry_dedupe_result(idempotency_key, retry_id)
        if idempotency_key
        else deepcopy(reference)
    )
    reference = canonical or reference
    _remember_inbound_call_reference(reference)
    _deliver_call_retry(retry_id, client=client)
    return {
        "owner_contact_id": str(reference.get("owner_contact_id") or ""),
        "task_id": str(reference.get("task_id") or ""),
        "call_id": str(reference.get("call_id") or ""),
        "work_event_id": str(reference.get("work_event_id") or ""),
    }


def record_contact_call_message(
    contact_id: str,
    *,
    peer_contact_id: str = "",
    speaker_kind: str,
    speaker_id: str,
    speaker_name: str,
    message: str,
    client: InterfaceClient | None = None,
    idempotency_key: str = "",
    terminal: bool = False,
) -> bool:
    """Append a real wire message to the exact peer-correlated call."""
    if _call_retry_dedupe_receipt(idempotency_key).get("kind") == "append":
        # The mutation was already accepted durably. In particular, a terminal
        # append may already have delivered and cleared its live correlation;
        # returning False here would make the wire caller create a phantom call.
        return True
    peer_contact_id = str(peer_contact_id or contact_id)
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        correlation = deepcopy(
            contact.get("pending_calls", {}).get(peer_contact_id) or {}
        )
    if not (
        isinstance(correlation, dict)
        and not correlation.get("terminal_requested")
        and (
            _has_call_reference(correlation, "outbound")
            or _has_call_reference(correlation, "inbound")
        )
    ):
        return False
    role = _call_role_for_owner(correlation, contact_id)
    is_response = (
        role == "outbound" and speaker_kind == "silicon"
    ) or (
        role == "inbound" and speaker_kind == "manager"
    )
    appended = _append_correlated_call(
        correlation,
        speaker_kind=speaker_kind,
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        message=message,
        client=client,
        dedupe_key=idempotency_key,
        terminal=terminal and is_response,
    )
    return appended
