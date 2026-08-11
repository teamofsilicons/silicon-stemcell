"""Local snapshots of tasks, events and calls, so a manager can be told what it
already knows without a round trip.
"""
from __future__ import annotations

from interface.work import identity as identity_module
from interface.work import store as store_module
import time
from copy import deepcopy
from typing import Any
from interface import (
    InterfaceClient,
)


def _task_cache(contact_id: str, task_id: str) -> dict[str, Any]:
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        task = contact["tasks"].get(task_id)
        return deepcopy(task) if isinstance(task, dict) else {}


def _standalone_call_cache(contact_id: str, call_id: str) -> dict[str, Any]:
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        call = contact["standalone_calls"].get(call_id)
        return deepcopy(call) if isinstance(call, dict) else {}


def _remember_task(contact_id: str, snapshot: dict[str, Any]) -> None:
    task_id = str(snapshot.get("task_id") or "")
    if not task_id:
        return
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        previous = contact["tasks"].get(task_id)
        cached = dict(previous) if isinstance(previous, dict) else {}
        for key in (
            "task_id",
            "room_id",
            "title",
            "description",
            "state",
            "revision",
            "estimate_seconds",
            "active_elapsed_seconds",
            "timer_state",
            "timer_pause_reason",
        ):
            if key in snapshot:
                cached[key] = deepcopy(snapshot[key])
        todos = cached.setdefault("todos", {})
        for todo in snapshot.get("todos") or []:
            if isinstance(todo, dict) and todo.get("todo_id"):
                todos[str(todo["todo_id"])] = deepcopy(todo)
        cached.setdefault("events", {})
        cached.setdefault("worker_groups", {})
        cached.setdefault("calls", {})
        cached["_cached_at"] = time.time()
        contact["tasks"][task_id] = cached
        if snapshot.get("state") not in {"completed", "failed", "cancelled"}:
            contact["active_task_id"] = task_id
        elif contact.get("active_task_id") == task_id:
            contact["active_task_id"] = ""
        store_module._write_state(state)


def _remember_event(contact_id: str, snapshot: dict[str, Any]) -> None:
    task_id = str(snapshot.get("task_id") or "")
    kind = str(snapshot.get("kind") or "")
    cached_at = time.time()
    if not task_id and kind == "call" and snapshot.get("call_id"):
        with store_module._state_guard():
            state = store_module._read_state()
            contact = store_module._contact_state(state, contact_id)
            call = deepcopy(snapshot)
            call["_cached_at"] = cached_at
            contact["standalone_calls"][str(snapshot["call_id"])] = call
            store_module._write_state(state)
        return
    if not task_id:
        return
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        task = contact["tasks"].setdefault(
            task_id,
            {
                "task_id": task_id,
                "todos": {},
                "events": {},
                "worker_groups": {},
                "calls": {},
            },
        )
        task.setdefault("events", {})
        task.setdefault("worker_groups", {})
        task.setdefault("calls", {})
        work_event_id = str(snapshot.get("work_event_id") or "")
        if work_event_id:
            task["events"][work_event_id] = deepcopy(snapshot)
        if kind == "worker_group" and snapshot.get("group_id"):
            task["worker_groups"][str(snapshot["group_id"])] = deepcopy(snapshot)
        if kind == "call" and snapshot.get("call_id"):
            call = deepcopy(snapshot)
            call["_cached_at"] = cached_at
            task["calls"][str(snapshot["call_id"])] = call
        task["_cached_at"] = cached_at
        store_module._write_state(state)


def _cached_call_snapshot(reference: dict[str, Any]) -> dict[str, Any]:
    owner = str(reference.get("owner_contact_id") or "")
    task_id = str(reference.get("task_id") or "")
    call_id = str(reference.get("call_id") or "")
    if task_id:
        calls = _task_cache(owner, task_id).get("calls") or {}
        value = calls.get(call_id) if isinstance(calls, dict) else {}
        return deepcopy(value) if isinstance(value, dict) else {}
    return _standalone_call_cache(owner, call_id)


def active_task_id(contact_id: str) -> str:
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        return str(contact.get("active_task_id") or "")


def refresh_task_snapshot(
    contact_id: str,
    task_id: str,
    *,
    client: InterfaceClient | None = None,
) -> dict[str, Any]:
    """Fetch Glass's current task and reconcile the local revision cache."""
    task_id = str(task_id or "")
    if not task_id:
        return {}
    try:
        snapshot = identity_module._result_data(
            (client or InterfaceClient()).work_task_show(task_id)
        )
    except Exception:
        return {}
    if not snapshot:
        return {}
    _remember_task(str(contact_id), snapshot)
    return deepcopy(snapshot)
