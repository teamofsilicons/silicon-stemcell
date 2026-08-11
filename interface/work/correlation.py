"""The record that mirrors one call across both managers.

An outbound call for one Silicon is an inbound call for another; this is what
keeps the two views of it in step, and what marks both terminal together.
"""
from __future__ import annotations

from interface.work import store as store_module
import time
from copy import deepcopy
from typing import Any


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


def _clear_call_correlations(call_id: str) -> None:
    with store_module._state_guard():
        state = store_module._read_state()
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
            store_module._write_state(state)


def _mark_call_correlations_terminal(call_id: str) -> None:
    with store_module._state_guard():
        state = store_module._read_state()
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
            store_module._write_state(state)


def _touch_call_correlations(call_ids: set[str]) -> bool:
    """Atomically claim activity unless inactivity already closed the call."""
    call_ids = {str(value or "") for value in call_ids if value}
    if not call_ids:
        return False
    with store_module._state_guard():
        state = store_module._read_state()
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
        store_module._write_state(state)
        return True


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
    with store_module._state_guard():
        state = store_module._read_state()
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
            store_module._write_state(state)
        return touched


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
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        contact["pending_calls"][target_id] = correlation
        store_module._write_state(state)


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
    with store_module._state_guard():
        state = store_module._read_state()
        recipient = store_module._contact_state(state, contact_id)
        recipient["pending_calls"][source_id] = deepcopy(correlation)
        outbound_owner = correlation["outbound_owner_contact_id"]
        if outbound_owner:
            owner = store_module._contact_state(state, outbound_owner)
            owner["pending_calls"][contact_id] = deepcopy(correlation)
        store_module._write_state(state)
