"""The long-task document, its leases, and what may be recovered from it.

A lease is what says a lifecycle is still owned by a live process. An entry
whose lease expired is recoverable; one that is merely old is not.
"""
from __future__ import annotations
from interface.long_tasks import constants
from interface.long_tasks import util as util_module
import os
import time
from copy import deepcopy
from typing import Any
from helpers.state import read_json, update_json


def _default_state() -> dict[str, Any]:
    return {"version": 2, "contacts": {}, "queued_roots": {}}


def _entry_has_durable_delivery(entry: dict[str, Any]) -> bool:
    """Return whether a persisted lifecycle still owns delivery work."""
    return bool(
        entry.get("pending_reply")
        or entry.get("pending_workers")
        or entry.get("pending_create_spec")
        or entry.get("settle_requested")
    )


def _entry_has_live_lease(
    entry: dict[str, Any],
    now: float | None = None,
) -> bool:
    now = float(now or time.time())
    return bool(
        entry.get("lease_owner")
        and float(entry.get("lease_until") or 0) > now
        and util_module._pid_alive(entry.get("lease_pid"))
    )


def _entry_has_effective_live_lease(
    entry: dict[str, Any],
    now: float | None = None,
) -> bool:
    """Ignore a prior process lease when the container reused our PID."""
    if not _entry_has_live_lease(entry, now):
        return False
    try:
        lease_pid = int(entry.get("lease_pid") or 0)
    except (TypeError, ValueError):
        return False
    owner = str(entry.get("lease_owner") or "")
    return not (
        lease_pid == os.getpid()
        and not owner.startswith(f"{constants._PROCESS_TOKEN}:")
    )


def _recoverable_terminal_entry(
    entry: dict[str, Any],
    now: float | None = None,
) -> bool:
    """Identify an expired terminal fence with nothing left to deliver."""
    return bool(
        entry.get("active")
        and entry.get("terminal")
        and not _entry_has_durable_delivery(entry)
        and not _entry_has_live_lease(entry, now)
    )


def _recoverable_empty_ephemeral_entry(
    entry: dict[str, Any],
    now: float | None = None,
) -> bool:
    """Identify a finished manager-only lifecycle left behind by a restart."""
    return bool(
        entry.get("active")
        and not entry.get("task_id")
        and not entry.get("manager_running")
        and not _entry_has_durable_delivery(entry)
        and not _entry_has_effective_live_lease(entry, now)
    )


def _tombstone(entry: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = float(now or time.time())
    return {
        "active": False,
        "run_fingerprint": util_module._fingerprint(entry.get("run_id")),
        "task_fingerprint": util_module._fingerprint(entry.get("task_id")),
        "settled_at": now,
        "updated_at": now,
    }


def _prune_state_locked(state: dict[str, Any], now: float | None = None) -> None:
    """Bound retained state without discarding recent live delivery intent."""
    now = float(now or time.time())
    state["version"] = 2
    contacts = state.setdefault("contacts", {})
    if not isinstance(contacts, dict):
        state["contacts"] = {}
        return

    for contact_id, raw in list(contacts.items()):
        if not isinstance(raw, dict):
            contacts.pop(contact_id, None)
            continue
        updated = float(raw.get("updated_at") or 0)
        if raw.get("active") and updated and now - updated > constants.STALE_ACTIVE_SECONDS:
            contacts[contact_id] = _tombstone(raw, now)
            continue
        if not raw.get("active"):
            # Old versions retained titles/descriptions in inactive entries.
            if set(raw) - {
                "active",
                "run_fingerprint",
                "task_fingerprint",
                "settled_at",
                "updated_at",
            }:
                raw = contacts[contact_id] = _tombstone(raw, now)
            settled = float(raw.get("settled_at") or raw.get("updated_at") or 0)
            if settled and now - settled > constants.TOMBSTONE_SECONDS:
                contacts.pop(contact_id, None)

    if len(contacts) <= constants.MAX_STATE_CONTACTS:
        pass
    else:
        inactive = sorted(
            (
                (float(raw.get("updated_at") or 0), contact_id)
                for contact_id, raw in contacts.items()
                if isinstance(raw, dict) and not raw.get("active")
            )
        )
        for _, contact_id in inactive:
            if len(contacts) <= constants.MAX_STATE_CONTACTS:
                break
            contacts.pop(contact_id, None)

    queued = state.setdefault("queued_roots", {})
    if not isinstance(queued, dict):
        state["queued_roots"] = {}
        return
    for contact_id, items in list(queued.items()):
        if not isinstance(items, list):
            queued.pop(contact_id, None)
            continue
        valid = [item for item in items if isinstance(item, dict)]
        if valid:
            # Never prune an accepted root. New roots are rejected before
            # crossing the bound so their maintenance admission can retry.
            queued[contact_id] = valid
        else:
            queued.pop(contact_id, None)


def _state_entry(contact_id: str) -> dict[str, Any]:
    state = read_json(constants.LONG_TASK_STATE_FILE, _default_state())
    contacts = state.get("contacts") if isinstance(state, dict) else {}
    entry = contacts.get(str(contact_id)) if isinstance(contacts, dict) else {}
    return deepcopy(entry) if isinstance(entry, dict) else {}


def _recover_expired_terminal_entry(contact_id: str) -> bool:
    """Atomically tombstone one expired terminal lifecycle."""
    recovered = False
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        nonlocal recovered
        _prune_state_locked(state, now)
        contacts = state.setdefault("contacts", {})
        entry = contacts.get(str(contact_id))
        if (
            isinstance(entry, dict)
            and _recoverable_terminal_entry(entry, now)
        ):
            contacts[str(contact_id)] = _tombstone(entry, now)
            recovered = True

    update_json(constants.LONG_TASK_STATE_FILE, _default_state(), mutate)
    return recovered


def _recover_empty_ephemeral_entry(contact_id: str) -> bool:
    """Atomically tombstone one expired lifecycle with nothing to recover."""
    recovered = False
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        nonlocal recovered
        _prune_state_locked(state, now)
        contacts = state.setdefault("contacts", {})
        entry = contacts.get(str(contact_id))
        if (
            isinstance(entry, dict)
            and _recoverable_empty_ephemeral_entry(entry, now)
        ):
            contacts[str(contact_id)] = _tombstone(entry, now)
            recovered = True

    update_json(constants.LONG_TASK_STATE_FILE, _default_state(), mutate)
    return recovered


def _active_entries() -> list[tuple[str, dict[str, Any]]]:
    state = read_json(constants.LONG_TASK_STATE_FILE, _default_state())
    contacts = state.get("contacts") if isinstance(state, dict) else {}
    if not isinstance(contacts, dict):
        return []
    entries = [
        (str(contact_id), deepcopy(entry))
        for contact_id, entry in contacts.items()
        if isinstance(entry, dict) and entry.get("active")
    ]
    entries.sort(
        key=lambda item: float(item[1].get("updated_at") or 0),
        reverse=True,
    )
    return entries


def _claim_contact(
    contact_id: str,
    owner: str,
    *,
    expected_run_id: str = "",
    allow_create: bool,
) -> dict[str, Any] | None:
    claimed: dict[str, Any] | None = None
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        nonlocal claimed
        _prune_state_locked(state, now)
        contacts = state.setdefault("contacts", {})
        entry = contacts.get(str(contact_id))
        if not isinstance(entry, dict) or not entry.get("active"):
            if not allow_create:
                return
            active_count = sum(
                1
                for item in contacts.values()
                if isinstance(item, dict) and item.get("active")
            )
            if active_count >= constants.MAX_ACTIVE_CONTACTS:
                return
            entry = {
                "active": True,
                "contact_id": str(contact_id),
                "run_id": str(expected_run_id),
                "updated_at": now,
            }
            contacts[str(contact_id)] = entry
        lease_owner = str(entry.get("lease_owner") or "")
        lease_until = float(entry.get("lease_until") or 0)
        lease_pid = entry.get("lease_pid")
        if (
            lease_owner
            and lease_owner != owner
            and lease_until > now
            and util_module._pid_alive(lease_pid)
        ):
            return
        entry["lease_owner"] = owner
        entry["lease_pid"] = os.getpid()
        entry["lease_until"] = now + constants.LEASE_SECONDS
        entry["updated_at"] = now
        claimed = deepcopy(entry)

    update_json(constants.LONG_TASK_STATE_FILE, _default_state(), mutate)
    return claimed
