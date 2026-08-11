"""The durable retry journal: what is owed, and what has been paid.

Capacity is bounded and eviction is deliberate — a full journal drops the
oldest archived entry, never the newest owed one. Idempotency receipts stop a
retry being journalled twice.
"""
from __future__ import annotations
from interface import contacts as contacts_module
from interface import state as interface_state

from interface.work import constants
from interface.work import identity as identity_module
from interface.work import payloads as payloads_module
from interface.work import store as store_module
import time
from copy import deepcopy
from typing import Any


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
    while len(journal) + new_count > constants.CALL_RETRY_MAX_ENTRIES:
        now = time.time()
        victim = _call_retry_capacity_victim(journal, now=now)
        if not isinstance(victim, dict):
            state["call_retry_overflow_count"] = (
                int(state.get("call_retry_overflow_count") or 0) + 1
            )
            state["call_retry_last_overflow_at"] = now
            raise constants.WorkUpdateError(
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
        archive.append(store_module._call_retry_archive_record(victim, now))
        del archive[:-constants.CALL_RETRY_ARCHIVE_LIMIT]
        journal.pop(str(victim.get("retry_id") or ""), None)
    for source in entries:
        retry_id = str(source.get("retry_id") or "")
        if not retry_id:
            raise constants.WorkUpdateError("Call journal entry requires retry_id.")
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
    with store_module._state_guard():
        state = store_module._read_state()
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
            store_module._write_state(state)
    return inserted


def _call_retry_dedupe_receipt(dedupe_key: str) -> dict[str, Any]:
    """Return one body-free idempotency receipt, including legacy kind inference."""
    if not dedupe_key:
        return {}
    with store_module._state_guard():
        state = store_module._read_state()
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
    with store_module._state_guard():
        state = store_module._read_state()
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
            (interface_state.get_contact(owner) or {}).get("room_id") or ""
        )
    if direction == "outbound":
        stable_reference["speaker_name"] = str(
            contacts_module.get_own_profile().get("name") or "Silicon manager"
        )
    occurred_at = identity_module._utc_now()
    retry_id = payloads_module._call_retry_id(direction, stable_reference)
    payload = payloads_module._call_create_payload(
        direction,
        stable_reference,
        target_name=target_name,
        message=message,
        occurred_at=occurred_at,
    )
    entry = payloads_module._new_call_retry_entry(
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


def _journal_call_patch(
    reference: dict[str, Any],
    payload: dict[str, Any],
    *,
    mutation_id: str = "",
    direction: str = "mutation",
    occurred_at: str = "",
) -> str:
    mutation_id = str(
        mutation_id or identity_module._new_id("call-mutation", reference.get("call_id"))
    )
    entry = payloads_module._call_patch_entry(
        reference,
        payload,
        mutation_id=mutation_id,
        direction=direction,
        occurred_at=occurred_at,
    )
    return _insert_call_retry_entries([entry])[0]


def _call_retry_entry(retry_id: str) -> dict[str, Any]:
    with store_module._state_guard():
        state = store_module._read_state()
        return deepcopy(state.get("call_retry_journal", {}).get(retry_id) or {})
