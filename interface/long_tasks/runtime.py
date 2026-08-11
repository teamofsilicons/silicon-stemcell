"""Starting a lifecycle, and rebuilding the ones a restart interrupted.
"""
from __future__ import annotations
from interface.long_tasks import constants
from interface.long_tasks import lifecycle as lifecycle_module
from interface.long_tasks import registry as registry_module
from interface.long_tasks import store as store_module
from interface.long_tasks import util as util_module
import time
import uuid
from copy import deepcopy
from typing import Any, Callable


def begin_long_task_run(
    contact_id: str,
    run_id: str,
    context: str,
    *,
    visible: bool,
    activity_heartbeat: Callable[[str], None] | None = None,
    reply_sender: Callable[..., str] | None = None,
    has_active_workers: Callable[[str], bool] | None = None,
    worker_status_resolver: Callable[[str, str], str] | None = None,
) -> lifecycle_module.LongTaskLifecycle | None:
    """Start an invisible-or-visible lifecycle or attach its continuation."""
    contact_id = str(contact_id)
    with registry_module._REGISTRY_LOCK:
        current = registry_module._ACTIVE_BY_CONTACT.get(contact_id)
        if current is not None and current.is_open:
            current.reply_sender = reply_sender or current.reply_sender
            current.has_active_workers = (
                has_active_workers or current.has_active_workers
            )
            current.worker_status_resolver = (
                worker_status_resolver or current.worker_status_resolver
            )
            current.attach(run_id, context, activity_heartbeat)
            return current
        saved = store_module._state_entry(contact_id)
        lifecycle = lifecycle_module.LongTaskLifecycle(
            contact_id,
            run_id,
            context,
            activity_heartbeat=activity_heartbeat,
            saved=saved if saved.get("active") else None,
            reply_sender=reply_sender,
            has_active_workers=has_active_workers,
            worker_status_resolver=worker_status_resolver,
        )
        if not lifecycle.is_open:
            return None
        registry_module._ACTIVE_BY_CONTACT[contact_id] = lifecycle
        return lifecycle


def recover_long_task_lifecycles(
    *,
    reply_sender: Callable[..., str] | None = None,
    has_active_workers: Callable[[str], bool] | None = None,
    worker_status_resolver: Callable[[str, str], str] | None = None,
    limit: int = constants.MAX_RECOVERY_CONTACTS,
) -> int:
    """Claim and replay a bounded set of active journals at process boot."""
    recovered = 0
    entries = store_module._active_entries()[: max(0, min(int(limit), constants.MAX_RECOVERY_CONTACTS))]
    for contact_id, saved in entries:
        if (
            store_module._recoverable_empty_ephemeral_entry(saved)
            and store_module._recover_empty_ephemeral_entry(contact_id)
        ):
            continue
        lifecycle: lifecycle_module.LongTaskLifecycle | None = None
        with registry_module._REGISTRY_LOCK:
            current = registry_module._ACTIVE_BY_CONTACT.get(contact_id)
            if current is not None and current.is_open:
                continue
            owner = f"{constants._PROCESS_TOKEN}:{uuid.uuid4().hex}"
            claimed = store_module._claim_contact(
                contact_id,
                owner,
                expected_run_id=str(saved.get("run_id") or ""),
                allow_create=False,
            )
            if claimed is None:
                continue
            lifecycle = lifecycle_module.LongTaskLifecycle(
                contact_id,
                str(saved.get("run_id") or ""),
                "",
                saved=claimed,
                lease_owner=owner,
                recovery=True,
                auto_start=False,
                reply_sender=reply_sender,
                has_active_workers=has_active_workers,
                worker_status_resolver=worker_status_resolver,
            )
            if not lifecycle.is_open:
                continue
            registry_module._ACTIVE_BY_CONTACT[contact_id] = lifecycle
            recovered += 1
        # Claiming is synchronous and bounded; transports replay on one daemon
        # per contact. New roots observe the active journal and are durably
        # queued, so startup never waits on N sequential network timeouts.
        lifecycle.start()
    return recovered


def _persisted_active_estimated_task_snapshots(
    *,
    limit: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Read legacy active task cache entries that predate lifecycle journals."""
    try:
        from interface.work import store as work_store

        with work_store._state_guard():
            state = work_store._read_state()
    except Exception:
        return []
    contacts = state.get("contacts")
    if not isinstance(contacts, dict):
        return []
    found: list[tuple[str, dict[str, Any]]] = []
    for contact_id, contact in sorted(contacts.items()):
        if len(found) >= max(0, min(int(limit), constants.MAX_RECOVERY_CONTACTS)):
            break
        if not isinstance(contact, dict):
            continue
        task_id = str(contact.get("active_task_id") or "")
        tasks = contact.get("tasks")
        snapshot = tasks.get(task_id) if isinstance(tasks, dict) else None
        if not task_id or not isinstance(snapshot, dict):
            continue
        if str(snapshot.get("state") or "") in constants._TERMINAL_STATES:
            continue
        estimate_present, goal_seconds = util_module._estimate_goal_from_data(snapshot)
        if not estimate_present or not goal_seconds:
            continue
        item = deepcopy(snapshot)
        item["task_id"] = task_id
        found.append((str(contact_id), item))
    return found


def backfill_active_estimated_task_lifecycles(
    *,
    reply_sender: Callable[..., str] | None = None,
    has_active_workers: Callable[[str], bool] | None = None,
    worker_status_resolver: Callable[[str, str], str] | None = None,
    limit: int = constants.MAX_RECOVERY_CONTACTS,
) -> int:
    """Adopt cached active estimated tasks created before journaling existed."""
    backfilled = 0
    snapshots = _persisted_active_estimated_task_snapshots(limit=limit)
    for contact_id, snapshot in snapshots:
        task_id = str(snapshot.get("task_id") or "")
        _, goal_seconds = util_module._estimate_goal_from_data(snapshot)
        lifecycle: lifecycle_module.LongTaskLifecycle | None = None
        created = False
        changed = False
        with registry_module._REGISTRY_LOCK:
            current = registry_module._ACTIVE_BY_CONTACT.get(contact_id)
            if current is not None and current.is_open:
                lifecycle = current
            else:
                lifecycle = lifecycle_module.LongTaskLifecycle(
                    contact_id,
                    util_module._stable_id("backfill-run", contact_id, task_id),
                    "",
                    auto_start=False,
                    recovery=True,
                    reply_sender=reply_sender,
                    has_active_workers=has_active_workers,
                    worker_status_resolver=worker_status_resolver,
                )
                if not lifecycle.is_open:
                    continue
                registry_module._ACTIVE_BY_CONTACT[contact_id] = lifecycle
                created = True

            with lifecycle._lock:
                if lifecycle._terminal or (
                    lifecycle.task_id
                    and lifecycle.task_id != task_id
                ):
                    continue
                changed = (
                    created
                    or not lifecycle.task_confirmed
                    or lifecycle.task_id != task_id
                    or not lifecycle.accuracy_schedule
                )
                lifecycle.task_id = task_id
                lifecycle.task_confirmed = True
                lifecycle.title = util_module._compact(
                    snapshot.get("title") or lifecycle.title,
                    120,
                )
                lifecycle.base_description = util_module._compact(
                    snapshot.get("description")
                    or lifecycle.base_description,
                    1_500,
                )
                lifecycle._last_durable_description = str(
                    snapshot.get("description") or ""
                )
                todos = snapshot.get("todos")
                if not lifecycle.todo_id:
                    if isinstance(todos, dict):
                        lifecycle.todo_id = str(
                            next(iter(todos), "")
                        )
                    elif isinstance(todos, list):
                        lifecycle.todo_id = str(
                            next(
                                (
                                    item.get("todo_id")
                                    for item in todos
                                    if isinstance(item, dict)
                                    and item.get("todo_id")
                                ),
                                "",
                            )
                        )
                elapsed = util_module._non_negative_number(
                    snapshot.get("active_elapsed_seconds")
                )
                interval = goal_seconds / constants.ACCURACY_REVIEW_SEGMENTS
                anchor_at = time.time() - (elapsed or interval)
                changed = (
                    lifecycle._set_accuracy_goal_locked(
                        task_id,
                        goal_seconds,
                        now=anchor_at,
                    )
                    or changed
                )
                lifecycle._persist(active=True)
                if changed:
                    backfilled += 1
        lifecycle.start()
    return backfilled
