"""Being called by another Silicon: preparing, enqueuing, recording.
"""
from __future__ import annotations

from interface.work import cache as cache_module
from interface.work import correlation as correlation_module
from interface.work import delivery as delivery_module
from interface.work import identity as identity_module
from interface.work import journal as journal_module
from copy import deepcopy
from typing import Any
from interface import (
    InterfaceClient,
)


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
    task_id = cache_module.active_task_id(contact_id)
    call_id = identity_module._new_id("call", source_id)
    work_event_id = identity_module._stable_id(
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
    retry_id = journal_module._journal_call_create(
        "inbound",
        reference,
        dedupe_key=idempotency_key,
    )
    canonical = (
        journal_module._call_retry_dedupe_result(idempotency_key, retry_id)
        if idempotency_key
        else deepcopy(reference)
    )
    reference = canonical or reference
    correlation_module._remember_inbound_call_reference(reference)
    delivery_module._schedule_call_retry(retry_id, client=client)
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
    retry_id = journal_module._journal_call_create(
        "inbound",
        reference,
        dedupe_key=idempotency_key,
    )
    canonical = (
        journal_module._call_retry_dedupe_result(idempotency_key, retry_id)
        if idempotency_key
        else deepcopy(reference)
    )
    reference = canonical or reference
    correlation_module._remember_inbound_call_reference(reference)
    delivery_module._deliver_call_retry(retry_id, client=client)
    return {
        "owner_contact_id": str(reference.get("owner_contact_id") or ""),
        "task_id": str(reference.get("task_id") or ""),
        "call_id": str(reference.get("call_id") or ""),
        "work_event_id": str(reference.get("work_event_id") or ""),
    }
