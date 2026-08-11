"""Recording what a worker is doing, on the card its manager is watching.
"""
from __future__ import annotations

from interface.work import constants
from interface.work import cache as cache_module
from interface.work import identity as identity_module
from interface.work import store as store_module
from interface.work import updates as updates_module
from copy import deepcopy
from interface import (
    InterfaceClient,
)


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
    task_id = str(task_id or cache_module.active_task_id(contact_id) or "")
    if not task_id:
        return {}
    group_id = identity_module._stable_id("worker-group", contact_id, task_id)
    invocation_id = str(invocation_id or identity_module._new_id("invocation", worker_id))
    worker_state = str(
        state_name or ("yet_to_start" if queued else "in_progress")
    )
    if worker_state not in constants.WORKER_STATES:
        worker_state = "yet_to_start" if queued else "in_progress"
    try:
        updates = updates_module.WorkUpdates(contact_id, client=client)
    except Exception:
        return {}
    cached = cache_module._task_cache(contact_id, task_id)
    if group_id not in (cached.get("worker_groups") or {}):
        try:
            updates.execute(
                {
                    "action": "worker-group/create",
                    "task_id": task_id,
                    "data": {
                        "group_id": group_id,
                        "work_event_id": identity_module._stable_id(
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
                    "name": identity_module._compact(description, 500) or worker_id,
                    "description": (
                        identity_module._compact(state_description, 500)
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
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        contact["workers"][worker_id] = {
            "task_id": task_id,
            "group_id": group_id,
            "invocation_id": invocation_id,
            "state": worker_state,
        }
        store_module._write_state(state)
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
    if state_name not in constants.WORKER_STATES:
        return False
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        correlation = deepcopy(contact.get("workers", {}).get(worker_id) or {})
    if not correlation:
        try:
            from interface.long_tasks import record_pending_worker_state

            return record_pending_worker_state(
                contact_id,
                worker_id,
                state_name,
                description,
            )
        except Exception:
            return False
    try:
        updates_module.WorkUpdates(contact_id, client=client).execute(
            {
                "action": "worker/update",
                "task_id": correlation["task_id"],
                "group_id": correlation["group_id"],
                "invocation_id": correlation["invocation_id"],
                "data": {
                    "state": state_name,
                    "description": identity_module._compact(description, 500),
                },
            }
        )
    except Exception:
        try:
            from interface.long_tasks import record_pending_worker_state

            return record_pending_worker_state(
                contact_id,
                worker_id,
                state_name,
                description,
            )
        except Exception:
            return False
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        current = contact.get("workers", {}).get(worker_id)
        if isinstance(current, dict):
            if state_name in {"completed", "failed", "cancelled"}:
                contact.get("workers", {}).pop(worker_id, None)
            else:
                current["state"] = state_name
            store_module._write_state(state)
    return True
