"""The lifecycles this process currently owns, by contact.
"""
from __future__ import annotations
from interface.long_tasks import constants
from interface.long_tasks import lifecycle as lifecycle_module
from interface.long_tasks import store as store_module
from interface.long_tasks import util as util_module
import threading
import time
from typing import Any
from helpers.state import update_json


_REGISTRY_LOCK = threading.RLock()


_ACTIVE_BY_CONTACT: dict[str, "lifecycle_module.LongTaskLifecycle"] = {}


def current_long_task(contact_id: str) -> lifecycle_module.LongTaskLifecycle | None:
    with _REGISTRY_LOCK:
        lifecycle = _ACTIVE_BY_CONTACT.get(str(contact_id))
        return lifecycle if lifecycle is not None and lifecycle.is_open else None


def _unregister(lifecycle: lifecycle_module.LongTaskLifecycle) -> None:
    with _REGISTRY_LOCK:
        current = _ACTIVE_BY_CONTACT.get(lifecycle.contact_id)
        if current is lifecycle:
            _ACTIVE_BY_CONTACT.pop(lifecycle.contact_id, None)


def reset_long_task_registry_for_tests() -> None:
    """Stop daemon lifecycles and release their leases between unit tests."""
    with _REGISTRY_LOCK:
        lifecycles = list(_ACTIVE_BY_CONTACT.values())
        _ACTIVE_BY_CONTACT.clear()
    for lifecycle in lifecycles:
        with lifecycle._lock:
            lifecycle._closed = True
            lifecycle._stop.set()
            now = time.time()

            contact_id = lifecycle.contact_id
            lease_owner = lifecycle._lease_owner

            def mutate(
                state: dict[str, Any],
                *,
                contact_id: str = contact_id,
                lease_owner: str = lease_owner,
                now: float = now,
            ) -> None:
                entry = (state.get("contacts") or {}).get(
                    contact_id
                )
                if (
                    isinstance(entry, dict)
                    and entry.get("lease_owner") == lease_owner
                ):
                    entry["lease_until"] = now - 1
                    entry["lease_pid"] = 0

            update_json(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)


def record_pending_worker_state(
    contact_id: str,
    worker_id: str,
    state_name: str,
    description: str = "",
) -> bool:
    """Retain actual worker truth while its initial card is replaying."""
    lifecycle = current_long_task(contact_id)
    if lifecycle is not None:
        return lifecycle.record_pending_worker_state(
            worker_id, state_name, description
        )
    updated = False
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        nonlocal updated
        entry = (state.get("contacts") or {}).get(str(contact_id))
        if not isinstance(entry, dict) or not entry.get("active"):
            return
        intent = (entry.get("pending_workers") or {}).get(str(worker_id))
        if not isinstance(intent, dict):
            return
        intent["phase"] = "launched"
        intent["state"] = str(state_name)
        if description:
            intent["state_description"] = util_module._compact(description, 500)
        intent["next_attempt_at"] = 0.0
        intent["fact_updated_at"] = now
        entry["updated_at"] = now
        updated = True

    update_json(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)
    return updated
