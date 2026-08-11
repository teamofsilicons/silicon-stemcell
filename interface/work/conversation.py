"""Appending a Silicon-to-Silicon message to the call it belongs to.
"""
from __future__ import annotations

from interface.work import correlation as correlation_module
from interface.work import delivery as delivery_module
from interface.work import identity as identity_module
from interface.work import journal as journal_module
from interface.work import payloads as payloads_module
from interface.work import store as store_module
from copy import deepcopy
from typing import Any
from interface import (
    InterfaceClient,
)


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
    if not correlation_module._touch_call_correlations(call_ids):
        return False
    message_id = (
        identity_module._stable_id("call-message", dedupe_key)
        if dedupe_key
        else identity_module._new_id("call-message")
    )
    occurred_at = identity_module._utc_now()
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
            "transcript_id": identity_module._stable_id(
                "transcript-message",
                message_id,
                side,
            ),
            "speaker_kind": (
                "silicon" if speaker_kind == "silicon" else "manager"
            ),
            "speaker_id": identity_module._safe_fragment(speaker_id, "speaker"),
            "speaker_name": speaker_name or speaker_id,
            "body": message,
            "blocks": [],
            "revision": 0,
            "created_at": occurred_at,
            "updated_at": occurred_at,
        }
        entries.append(
            payloads_module._call_patch_entry(
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
    receipt = journal_module._call_retry_dedupe_receipt(dedupe_key)
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
                delivery_module._deliver_call_retry(retry_id, client=client)
            else:
                delivery_module._schedule_call_retry(retry_id, client=client)
        return True
    retry_ids = journal_module._insert_call_retry_entries(
        entries,
        dedupe_key=dedupe_key,
        dedupe_kind="append",
    )
    if terminal:
        for entry in entries:
            correlation_module._mark_call_correlations_terminal(
                str((entry.get("reference") or {}).get("call_id") or "")
            )
    for retry_id in retry_ids:
        if synchronous:
            delivery_module._deliver_call_retry(retry_id, client=client)
        else:
            delivery_module._schedule_call_retry(retry_id, client=client)
    # Once durable, this is accepted even if either remote side is unavailable.
    return True


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
    if journal_module._call_retry_dedupe_receipt(idempotency_key).get("kind") == "append":
        # The mutation was already accepted durably. In particular, a terminal
        # append may already have delivered and cleared its live correlation;
        # returning False here would make the wire caller create a phantom call.
        return True
    peer_contact_id = str(peer_contact_id or contact_id)
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        correlation = deepcopy(
            contact.get("pending_calls", {}).get(peer_contact_id) or {}
        )
    if not (
        isinstance(correlation, dict)
        and not correlation.get("terminal_requested")
        and (
            correlation_module._has_call_reference(correlation, "outbound")
            or correlation_module._has_call_reference(correlation, "inbound")
        )
    ):
        return False
    role = correlation_module._call_role_for_owner(correlation, contact_id)
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
