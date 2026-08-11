"""Building what goes on the wire, without taking a lock.

Called from inside a state guard, so nothing here may acquire one.
"""
from __future__ import annotations
from interface import contacts as contacts_module
from interface import state as interface_state

from interface.work import constants
from interface.work import identity as identity_module
import time
from copy import deepcopy
from typing import Any


def _call_retry_id(direction: str, reference: dict[str, Any]) -> str:
    call_id = str(reference.get("call_id") or "")
    return identity_module._stable_id(
        "call-create-retry",
        direction,
        reference.get("owner_contact_id"),
        call_id,
    )


def _call_retry_lane(reference: dict[str, Any]) -> str:
    return identity_module._stable_id(
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
    fallback = str(occurred_at or identity_module._utc_now())
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
        raise constants.WorkUpdateError("Call retry requires an owner and call_id.")
    occurred_at = str(occurred_at or identity_module._utc_now())
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
            or contacts_module.get_own_profile().get("name")
            or "Silicon manager"
        )
        transcript = [
            {
                "transcript_id": identity_module._stable_id("transcript-outbound", call_id),
                "speaker_kind": "manager",
                "speaker_id": identity_module._safe_fragment(f"manager:{owner}", "manager"),
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
                "transcript_id": identity_module._stable_id("transcript-inbound", call_id),
                "speaker_kind": target_kind,
                "speaker_id": identity_module._safe_fragment(
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
        or identity_module._stable_id("call-event", owner, task_id, call_id),
        "kind": "call",
        "direction": direction,
        "target_kind": target_kind,
        "target_id": identity_module._safe_fragment(target_id, "unknown"),
        "target_name": name,
        "state": "in_progress",
        "body": body,
        "blocks": [],
        "transcript": transcript,
        "client_id": identity_module._stable_id(
            "create-call",
            task_id or str(reference.get("room_id") or ""),
            call_id,
        ),
    }
    if not task_id:
        room_id = str(
            reference.get("room_id")
            or (interface_state.get_contact(owner) or {}).get("room_id")
            or ""
        )
        if not room_id:
            raise constants.WorkUpdateError("Standalone call retry requires a room.")
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
        raise constants.WorkUpdateError("Call mutation requires an owner and call_id.")
    occurred_at = str(occurred_at or identity_module._utc_now())
    stable_payload = deepcopy(payload)
    stable_payload.pop("revision", None)
    if "transcript" in stable_payload:
        stable_payload["transcript"] = _transcript_timestamps(
            stable_payload.get("transcript"),
            occurred_at=occurred_at,
        )
    stable_payload["client_id"] = identity_module._stable_id(
        "call-mutation",
        owner,
        call_id,
        mutation_id,
    )
    retry_id = identity_module._stable_id(
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
