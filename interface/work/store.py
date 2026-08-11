"""The work-updates document, and the one lock that guards it.

Every read and write goes through the same thread lock and the same file lock.
Retention pruning happens on read, in place, because a document an older
Stemcell wrote must be repaired rather than rejected.
"""
from __future__ import annotations

from interface.work import constants
from interface.work import identity as identity_module
import hashlib
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from helpers.state import file_lock, read_json, write_json


_STATE_LOCK = threading.RLock()


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


def _read_state_with_prune_status() -> tuple[dict[str, Any], bool]:
    raw_state = read_json(constants.WORK_UPDATES_FILE, _default_state())
    state = raw_state
    changed = False
    if not isinstance(state, dict):
        state = _default_state()
        changed = True
    defaults = _default_state()
    for key, value in defaults.items():
        if key not in state:
            state[key] = deepcopy(value)
            changed = True
    return state, _prune_state(state) or changed


def _read_state() -> dict[str, Any]:
    state, _changed = _read_state_with_prune_status()
    return state


def _write_state(state: dict[str, Any]) -> None:
    write_json(constants.WORK_UPDATES_FILE, state)


@contextmanager
def _state_guard():
    with _STATE_LOCK, file_lock(constants.WORK_UPDATES_FILE):
        yield


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


def _prune_state(state: dict[str, Any], now: float | None = None) -> bool:
    now = time.time() if now is None else float(now)
    changed = False
    dedupe = state.get("call_retry_dedupe")
    if not isinstance(dedupe, dict):
        state["call_retry_dedupe"] = {}
        changed = True
    else:
        for key, receipt in list(dedupe.items()):
            if (
                not isinstance(receipt, dict)
                or now - float(receipt.get("created_at") or 0.0)
                >= constants.CALL_RETRY_DEDUPE_RETENTION_SECONDS
            ):
                dedupe.pop(key, None)
                changed = True
        if len(dedupe) > constants.CALL_RETRY_DEDUPE_LIMIT:
            ordered = sorted(
                dedupe,
                key=lambda key: float(
                    (dedupe.get(key) or {}).get("created_at") or 0.0
                ),
            )
            for key in ordered[: len(dedupe) - constants.CALL_RETRY_DEDUPE_LIMIT]:
                dedupe.pop(key, None)
                changed = True
    journal = state.get("call_retry_journal")
    pending_call_ids: set[str] = set()
    if not isinstance(journal, dict):
        state["call_retry_journal"] = {}
        changed = True
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
                changed = True
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
                >= constants.CALL_RETRY_DEAD_LETTER_RETENTION_SECONDS
            ):
                archive = state.setdefault("call_retry_dead_letters", [])
                if not isinstance(archive, list):
                    archive = []
                    state["call_retry_dead_letters"] = archive
                archive.append(_call_retry_archive_record(entry, now))
                del archive[:-constants.CALL_RETRY_ARCHIVE_LIMIT]
                journal.pop(retry_id, None)
                changed = True
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
                    changed = True
                    continue
                updated_at = identity_module._timestamp(correlation.get("updated_at"))
                correlation_call_ids = {
                    str(correlation.get("outbound_call_id") or ""),
                    str(correlation.get("inbound_call_id") or ""),
                }
                if (
                    not correlation_call_ids.intersection(pending_call_ids)
                    and (
                        correlation.get("terminal_requested")
                        or not updated_at
                        or now - updated_at > constants.PENDING_CALL_TTL_SECONDS
                    )
                ):
                    pending.pop(peer_id, None)
                    changed = True

        standalone_calls = contact.get("standalone_calls")
        if isinstance(standalone_calls, dict):
            for call_id, call in list(standalone_calls.items()):
                if not isinstance(call, dict):
                    standalone_calls.pop(call_id, None)
                    changed = True
                    continue
                cached_at = identity_module._timestamp(call.get("_cached_at"))
                if (
                    call.get("state") in {"completed", "failed", "cancelled"}
                    and cached_at
                    and now - cached_at > constants.TERMINAL_TASK_TTL_SECONDS
                ):
                    standalone_calls.pop(call_id, None)
                    changed = True
            if len(standalone_calls) > 200:
                ordered = sorted(
                    (
                        (identity_module._timestamp(call.get("_cached_at")), call_id)
                        for call_id, call in standalone_calls.items()
                        if isinstance(call, dict)
                    )
                )
                for _cached_at, call_id in ordered[: len(standalone_calls) - 200]:
                    standalone_calls.pop(call_id, None)
                    changed = True

        tasks = contact.get("tasks")
        if not isinstance(tasks, dict):
            continue
        terminal = []
        for task_id, task in list(tasks.items()):
            if not isinstance(task, dict):
                tasks.pop(task_id, None)
                changed = True
                continue
            cached_at = identity_module._timestamp(task.get("_cached_at"))
            if (
                task.get("state") in {"completed", "failed", "cancelled"}
                and cached_at
                and now - cached_at > constants.TERMINAL_TASK_TTL_SECONDS
            ):
                tasks.pop(task_id, None)
                changed = True
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
                        changed = True
            terminal.append((cached_at, str(task_id)))
        if len(tasks) > constants.MAX_CACHED_TASKS_PER_CONTACT:
            active_id = str(contact.get("active_task_id") or "")
            for _cached_at, task_id in sorted(terminal):
                if len(tasks) <= constants.MAX_CACHED_TASKS_PER_CONTACT:
                    break
                if task_id != active_id:
                    tasks.pop(task_id, None)
                    changed = True
    return changed
