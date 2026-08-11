"""Accuracy-review roots: claiming one, dispatching it, and closing it out.
"""
from __future__ import annotations
from interface.long_tasks import constants
from interface.long_tasks import registry as registry_module
from interface.long_tasks import store as store_module
import time
from typing import Any
from helpers.state import update_json


def claim_ready_accuracy_review_roots(
    *,
    limit: int = 16,
    exclude_contacts: set[str] | None = None,
) -> dict[str, str]:
    """Claim due internal reviews for durable ManagerDispatcher admission."""
    excluded = {str(item) for item in (exclude_contacts or set())}
    owner = constants._PROCESS_TOKEN
    now = time.time()
    with registry_module._REGISTRY_LOCK:
        lifecycles = [
            lifecycle
            for contact_id, lifecycle in registry_module._ACTIVE_BY_CONTACT.items()
            if contact_id not in excluded and lifecycle.is_open
        ]
    lifecycles.sort(
        key=lambda lifecycle: float(
            (
                lifecycle.accuracy_schedule.get("pending_review") or {}
            ).get("created_at")
            or float("inf")
        )
    )
    claimed: dict[str, str] = {}
    for lifecycle in lifecycles:
        if len(claimed) >= max(0, min(int(limit), 64)):
            break
        result = lifecycle.claim_accuracy_review(owner=owner, now=now)
        if result is None:
            continue
        review_id, context = result
        claimed[lifecycle.contact_id] = (
            f"{constants._ACCURACY_REVIEW_MARKER} {review_id}\n{context}"
        )
    return claimed


def acknowledge_accuracy_review_dispatched(
    contact_id: str,
    review_id: str,
) -> bool:
    """Mark a review owned by MAINTENANCE without advancing its checkpoint."""
    contact_id = str(contact_id)
    review_id = str(review_id)
    lifecycle = registry_module.current_long_task(contact_id)
    if lifecycle is not None:
        return lifecycle.mark_accuracy_review_dispatched(review_id)
    updated = False

    def mutate(state: dict[str, Any]) -> None:
        nonlocal updated
        entry = (state.get("contacts") or {}).get(contact_id)
        schedule = (
            entry.get("accuracy_schedule")
            if isinstance(entry, dict)
            else None
        )
        pending = (
            schedule.get("pending_review")
            if isinstance(schedule, dict)
            else None
        )
        if (
            not isinstance(pending, dict)
            or pending.get("review_id") != review_id
        ):
            return
        pending["phase"] = "dispatched"
        pending["claim_until"] = 0.0
        schedule["updated_at"] = time.time()
        updated = True

    update_json(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)
    return updated


def close_terminal_accuracy_lifecycle(contact_id: str) -> bool:
    """Release a terminal task observed by an internal accuracy turn."""
    contact_id = str(contact_id)
    lifecycle = registry_module.current_long_task(contact_id)
    if lifecycle is not None:
        return lifecycle.close_if_terminal()
    closed = False
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        nonlocal closed
        contacts = state.get("contacts")
        entry = (
            contacts.get(contact_id)
            if isinstance(contacts, dict)
            else None
        )
        if (
            not isinstance(entry, dict)
            or not entry.get("active")
            or not entry.get("terminal")
            or entry.get("pending_reply")
            or entry.get("pending_workers")
            or entry.get("pending_create_spec")
            or entry.get("settle_requested")
        ):
            return
        contacts[contact_id] = store_module._tombstone(entry, now)
        closed = True

    update_json(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)
    return closed


def complete_accuracy_review_root(
    contact_id: str,
    review_id: str,
) -> bool:
    """Advance one recurring schedule only after its manager turn succeeds."""
    contact_id = str(contact_id)
    review_id = str(review_id)
    lifecycle = registry_module.current_long_task(contact_id)
    if lifecycle is not None:
        completed = lifecycle.complete_accuracy_review(review_id)
        lifecycle.close_if_terminal()
        return completed
    if close_terminal_accuracy_lifecycle(contact_id):
        return False
    updated = False

    def mutate(state: dict[str, Any]) -> None:
        nonlocal updated
        entry = (state.get("contacts") or {}).get(contact_id)
        schedule = (
            entry.get("accuracy_schedule")
            if isinstance(entry, dict)
            else None
        )
        pending = (
            schedule.get("pending_review")
            if isinstance(schedule, dict)
            else None
        )
        if (
            not isinstance(pending, dict)
            or pending.get("review_id") != review_id
        ):
            return
        schedule["next_checkpoint"] = max(
            int(schedule.get("next_checkpoint") or 1),
            int(pending.get("checkpoint_through") or 0) + 1,
        )
        schedule["pending_review"] = {}
        schedule["updated_at"] = time.time()
        updated = True

    update_json(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)
    return updated


def accuracy_review_root_is_current(
    contact_id: str,
    review_id: str,
) -> bool:
    """Return false after task terminalization cancels an admitted review."""
    contact_id = str(contact_id)
    review_id = str(review_id)
    lifecycle = registry_module.current_long_task(contact_id)
    if lifecycle is not None:
        return lifecycle.accuracy_review_is_current(review_id)
    entry = store_module._state_entry(contact_id)
    schedule = entry.get("accuracy_schedule")
    pending = (
        schedule.get("pending_review")
        if isinstance(schedule, dict)
        else None
    )
    return bool(
        entry.get("active")
        and not entry.get("terminal")
        and isinstance(pending, dict)
        and pending.get("review_id") == review_id
    )
