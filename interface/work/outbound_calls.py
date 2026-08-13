"""Calling another Silicon: preparing, enqueuing, recording.
"""
from __future__ import annotations
from interface import contacts as contacts_module

from interface.work import cache as cache_module
from interface.work import conversation as conversation_module
from interface.work import correlation as correlation_module
from interface.work import delivery as delivery_module
from interface.work import identity as identity_module
from interface.work import journal as journal_module
from interface.work import store as store_module
from copy import deepcopy
from typing import Any
from helpers.session import SILICON
from interface import (
    InterfaceClient,

)


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
    # The call's correlation is per peer, but the task it belongs to is the
    # session's, and the session's active task is filed under SILICON. Defaulting
    # it from the peer's own key found it back when the peer had a manager of its
    # own; now it is always empty and the call card loses its task.
    task_id = str(task_id or cache_module.active_task_id(SILICON) or "")
    target_kind = "silicon" if target_kind == "silicon" else "manager"
    with store_module._state_guard():
        state = store_module._read_state()
        contact_state = store_module._contact_state(state, contact_id)
        pending = deepcopy(
            contact_state.get("pending_calls", {}).get(str(target_id)) or {}
        )
    if not pending.get("terminal_requested") and (
        correlation_module._has_call_reference(pending, "outbound")
        or correlation_module._has_call_reference(pending, "inbound")
    ):
        local_reference = correlation_module._call_reference_for_owner(pending, contact_id)
        return {
            **local_reference,
            "target_kind": target_kind,
            "target_id": target_id,
            "continuation": True,
        }
    call_id = identity_module._new_id("call", target_id)
    return {
        "owner_contact_id": contact_id,
        "task_id": task_id,
        "call_id": call_id,
        "work_event_id": identity_module._stable_id(
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
    with store_module._state_guard():
        state = store_module._read_state()
        correlation = deepcopy(
            store_module._contact_state(state, owner)
            .get("pending_calls", {})
            .get(target_id)
            or {}
        )
    current_reference = correlation_module._call_reference_for_owner(correlation, owner)
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
        and journal_module._call_retry_dedupe_receipt(idempotency_key).get("kind") == "append"
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
        retry_id = journal_module._journal_call_create(
            "outbound",
            reference,
            target_name=target_name,
            message=message,
            dedupe_key=idempotency_key,
        )
        canonical = (
            journal_module._call_retry_dedupe_result(idempotency_key, retry_id)
            if idempotency_key
            else deepcopy(reference)
        )
        correlation_module._remember_outbound_call_reference(canonical or reference)
        delivery_module._schedule_call_retry(retry_id, client=client)
        return True
    owner = str(reference.get("owner_contact_id") or "")
    target_id = str(reference.get("target_id") or "")
    with store_module._state_guard():
        state = store_module._read_state()
        correlation = deepcopy(
            store_module._contact_state(state, owner)
            .get("pending_calls", {})
            .get(target_id)
            or {}
        )
    if not correlation:
        return False
    role = str(
        reference.get("continuation_role")
        or correlation_module._call_role_for_owner(correlation, owner)
    )
    return conversation_module._append_correlated_call(
        correlation,
        speaker_kind="manager",
        speaker_id=f"manager:{owner}",
        speaker_name=str(contacts_module.get_own_profile().get("name") or "Silicon manager"),
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
        retry_id = journal_module._journal_call_create(
            "outbound",
            reference,
            target_name=target_name,
            message=message,
        )
        correlation_module._remember_outbound_call_reference(reference)
        delivery_module._deliver_call_retry(retry_id, client=client)
        return reference
    owner = str(reference.get("owner_contact_id") or "")
    target_id = str(reference.get("target_id") or "")
    with store_module._state_guard():
        state = store_module._read_state()
        correlation = deepcopy(
            store_module._contact_state(state, owner)
            .get("pending_calls", {})
            .get(target_id)
            or {}
        )
    role = str(
        reference.get("continuation_role")
        or correlation_module._call_role_for_owner(correlation, owner)
    )
    if not correlation or not conversation_module._append_correlated_call(
        correlation,
        speaker_kind="manager",
        speaker_id=f"manager:{owner}",
        speaker_name=str(contacts_module.get_own_profile().get("name") or "Silicon manager"),
        message=message,
        client=client,
        synchronous=True,
        terminal=role == "inbound",
    ):
        return {}
    return reference
