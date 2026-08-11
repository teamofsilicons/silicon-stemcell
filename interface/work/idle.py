"""Closing calls that have gone quiet, on a deadline rather than a guess.
"""
from __future__ import annotations

from interface.work import constants
from interface.work import correlation as correlation_module
from interface.work import delivery as delivery_module
from interface.work import identity as identity_module
from interface.work import journal as journal_module
from interface.work import payloads as payloads_module
from interface.work import store as store_module
import time
from copy import deepcopy
from typing import Any
from interface import (
    InterfaceClient,
)


def next_inactive_call_deadline() -> float | None:
    """Return the next exact wall-clock deadline for idle call completion."""
    deadlines: list[float] = []
    with store_module._state_guard():
        state = store_module._read_state()
        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if isinstance(pending, dict):
                for correlation in pending.values():
                    if (
                        not isinstance(correlation, dict)
                        or correlation.get("terminal_requested")
                        or not correlation_module._correlation_references(correlation)
                    ):
                        continue
                    updated_at = identity_module._timestamp(correlation.get("updated_at"))
                    if updated_at:
                        deadlines.append(
                            updated_at + constants.CALL_IDLE_TIMEOUT_SECONDS
                        )
            for call in (
                contact.get("standalone_calls", {}) or {}
            ).values():
                if (
                    not isinstance(call, dict)
                    or call.get("_idle_terminal_requested")
                    or call.get("state")
                    not in {"connecting", "in_progress"}
                ):
                    continue
                cached_at = identity_module._timestamp(call.get("_cached_at"))
                if cached_at:
                    deadlines.append(cached_at + constants.CALL_IDLE_TIMEOUT_SECONDS)
            for task in (contact.get("tasks", {}) or {}).values():
                if not isinstance(task, dict):
                    continue
                task_cached_at = identity_module._timestamp(task.get("_cached_at"))
                for call in (task.get("calls", {}) or {}).values():
                    if (
                        not isinstance(call, dict)
                        or call.get("_idle_terminal_requested")
                        or call.get("state")
                        not in {"connecting", "in_progress"}
                    ):
                        continue
                    cached_at = identity_module._timestamp(
                        call.get("_cached_at") or task_cached_at
                    )
                    if cached_at:
                        deadlines.append(
                            cached_at + constants.CALL_IDLE_TIMEOUT_SECONDS
                        )
    return min(deadlines) if deadlines else None


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
    cutoff = now - constants.CALL_IDLE_TIMEOUT_SECONDS
    retry_ids: list[str] = []
    completed_count = 0

    with store_module._state_guard():
        state = store_module._read_state()
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
                identity = correlation_module._correlation_identity(correlation)
                references = correlation_module._correlation_references(correlation)
                if not any(identity) or not references:
                    continue
                for _side, reference in references:
                    correlated_references.add(
                        correlation_module._call_reference_identity(reference)
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
                    identity_module._timestamp(correlation.get("updated_at")),
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
            for side, reference in correlation_module._correlation_references(correlation):
                reference_identity = correlation_module._call_reference_identity(reference)
                if reference_identity in entry_identities:
                    continue
                entries.append(
                    payloads_module._call_patch_entry(
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
                identity = correlation_module._call_reference_identity(reference)
                if (
                    identity in correlated_references
                    or identity in entry_identities
                    or call.get("_idle_terminal_requested")
                    or call.get("state") not in {"connecting", "in_progress"}
                    or not identity_module._timestamp(call.get("_cached_at"))
                    or identity_module._timestamp(call.get("_cached_at")) > cutoff
                ):
                    continue
                entries.append(
                    payloads_module._call_patch_entry(
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
                task_cached_at = identity_module._timestamp(task.get("_cached_at"))
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
                    identity = correlation_module._call_reference_identity(reference)
                    cached_at = identity_module._timestamp(
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
                        payloads_module._call_patch_entry(
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
            retry_ids = journal_module._insert_call_retry_entries_in_state(state, entries)
        except Exception:
            store_module._write_state(state)
            raise
        for correlation in terminal_correlations:
            correlation["terminal_requested"] = True
            correlation["terminal_requested_at"] = now
            correlation["terminal_reason"] = "inactivity"
        for call in orphan_calls:
            call["_idle_terminal_requested"] = True
        store_module._write_state(state)

    for retry_id in retry_ids:
        delivery_module._schedule_call_retry(retry_id, client=client)
    return completed_count
