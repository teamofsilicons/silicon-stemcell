"""Durable work-update orchestration for Stemcell managers.

Glass owns canonical task state, timing, history, and chat events.  This module
only keeps the small amount of local correlation needed to reuse accepted
identities across manager rounds, workers, and progress frames.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

from core.background import submit_best_effort
from core.interface import (
    InterfaceClient,
    InterfaceError,
    STATE_DIR,
    get_contact,
    get_own_profile,
)
from core.state_store import file_lock, read_json, write_json


WORK_UPDATES_FILE = STATE_DIR / "work_updates.json"
_STATE_LOCK = threading.RLock()
PENDING_CALL_TTL_SECONDS = 6 * 60 * 60
TERMINAL_TASK_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_CACHED_TASKS_PER_CONTACT = 200

CANONICAL_ACTIVITY_STATES = {
    "thinking",
    "reading",
    "writing",
    "executing",
    "searching_web",
    "spawning_worker",
    "calling",
    "other",
    "done",
}
ACTIVITY_STATE_ALIASES = {
    "reading_file": "reading",
    "writing_file": "writing",
}
TERMINAL_ACTIONS = {
    "task/complete": ("complete", "completion", "completed"),
    "task/fail": ("fail", "failure", "failed"),
    "task/cancel": ("cancel", "cancellation", "cancelled"),
}
WORKER_STATES = {
    "yet_to_start",
    "in_progress",
    "completed",
    "blocked",
    "failed",
    "cancelled",
}
CALL_STATES = {"connecting", "in_progress", "completed", "failed", "cancelled"}


class WorkUpdateError(RuntimeError):
    """A manager-visible durable update failure."""


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "contacts": {},
    }


def _read_state() -> dict[str, Any]:
    state = read_json(WORK_UPDATES_FILE, _default_state())
    if not isinstance(state, dict):
        return _default_state()
    state.setdefault("version", 1)
    state.setdefault("contacts", {})
    _prune_state(state)
    return state


def _write_state(state: dict[str, Any]) -> None:
    write_json(WORK_UPDATES_FILE, state)


@contextmanager
def _state_guard():
    with _STATE_LOCK, file_lock(WORK_UPDATES_FILE):
        yield


def _timestamp(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _prune_state(state: dict[str, Any], now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    for contact in state.get("contacts", {}).values():
        if not isinstance(contact, dict):
            continue
        pending = contact.get("pending_calls")
        if isinstance(pending, dict):
            for peer_id, correlation in list(pending.items()):
                if not isinstance(correlation, dict):
                    pending.pop(peer_id, None)
                    continue
                updated_at = _timestamp(correlation.get("updated_at"))
                if not updated_at or now - updated_at > PENDING_CALL_TTL_SECONDS:
                    pending.pop(peer_id, None)

        tasks = contact.get("tasks")
        if not isinstance(tasks, dict):
            continue
        terminal = []
        for task_id, task in list(tasks.items()):
            if not isinstance(task, dict):
                tasks.pop(task_id, None)
                continue
            cached_at = _timestamp(task.get("_cached_at"))
            if (
                task.get("state") in {"completed", "failed", "cancelled"}
                and cached_at
                and now - cached_at > TERMINAL_TASK_TTL_SECONDS
            ):
                tasks.pop(task_id, None)
                continue
            for bucket_name, limit in (
                ("events", 500),
                ("calls", 200),
                ("worker_groups", 200),
                ("todos", 500),
            ):
                bucket = task.get(bucket_name)
                if isinstance(bucket, dict) and len(bucket) > limit:
                    for stale_id in list(bucket)[: len(bucket) - limit]:
                        bucket.pop(stale_id, None)
            terminal.append((cached_at, str(task_id)))
        if len(tasks) > MAX_CACHED_TASKS_PER_CONTACT:
            active_id = str(contact.get("active_task_id") or "")
            for _cached_at, task_id in sorted(terminal):
                if len(tasks) <= MAX_CACHED_TASKS_PER_CONTACT:
                    break
                if task_id != active_id:
                    tasks.pop(task_id, None)


def _contact_state(state: dict[str, Any], contact_id: str) -> dict[str, Any]:
    contacts = state.setdefault("contacts", {})
    contact = contacts.setdefault(
        contact_id,
        {
            "active_task_id": "",
            "activity": {},
            "tasks": {},
            "workers": {},
            "pending_calls": {},
        },
    )
    contact.setdefault("active_task_id", "")
    contact.setdefault("activity", {})
    contact.setdefault("tasks", {})
    contact.setdefault("workers", {})
    contact.setdefault("pending_calls", {})
    return contact


def _safe_fragment(value: Any, fallback: str = "item") -> str:
    cleaned = "".join(
        char if char.isalnum() or char in "._:-" else "-"
        for char in str(value or "").strip()
    ).strip("-._:")
    return (cleaned or fallback)[:48]


def _new_id(prefix: str, hint: Any = "") -> str:
    prefix = _safe_fragment(prefix, "work")
    hint = _safe_fragment(hint, "")
    token = uuid.uuid4().hex[:20]
    return f"{prefix}:{hint}:{token}"[:128] if hint else f"{prefix}:{token}"


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{_safe_fragment(prefix, 'work')}:{digest}"


def _compact(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _result_data(value: Any) -> dict[str, Any]:
    current = value
    for _ in range(3):
        if not isinstance(current, dict):
            return {}
        for key in ("data", "result"):
            nested = current.get(key)
            if isinstance(nested, dict):
                current = nested
                break
        else:
            return current
    return current if isinstance(current, dict) else {}


def _public_result(value: Any) -> str:
    data = _result_data(value)
    if data:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return _compact(value, limit=2_000)


def _has_call_reference(correlation: dict[str, Any], prefix: str) -> bool:
    return all(
        correlation.get(f"{prefix}_{field}")
        for field in ("owner_contact_id", "task_id", "call_id")
    )


def _call_reference_for_owner(
    correlation: dict[str, Any],
    contact_id: str,
) -> dict[str, str]:
    """Return only the call card owned by this manager/contact."""
    for prefix in ("outbound", "inbound"):
        if (
            _has_call_reference(correlation, prefix)
            and str(correlation.get(f"{prefix}_owner_contact_id") or "")
            == str(contact_id)
        ):
            return {
                "owner_contact_id": str(
                    correlation.get(f"{prefix}_owner_contact_id") or ""
                ),
                "task_id": str(correlation.get(f"{prefix}_task_id") or ""),
                "call_id": str(correlation.get(f"{prefix}_call_id") or ""),
            }
    return {}


def canonical_activity_state(state: str) -> str:
    normalized = ACTIVITY_STATE_ALIASES.get(str(state or ""), str(state or ""))
    return normalized if normalized in CANONICAL_ACTIVITY_STATES else "other"


def begin_manager_activity(contact_id: str, run_id: str = "") -> str:
    """Start or recover the stable manager-activity group for one inbound run."""
    seed = str(run_id or uuid.uuid4().hex)
    group_id = f"manager-run:{_safe_fragment(seed, uuid.uuid4().hex)}"
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        current = contact.get("activity")
        if not isinstance(current, dict) or current.get("run_id") != seed:
            contact["activity"] = {
                "run_id": seed,
                "group_id": group_id,
                "sequence": 0,
                "frames": {},
                "settled": False,
            }
            _write_state(state)
        else:
            group_id = str(current.get("group_id") or group_id)
    return group_id


def current_manager_activity_group(contact_id: str) -> str:
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        activity = contact.get("activity")
        if not isinstance(activity, dict) or activity.get("settled"):
            return ""
        return str(activity.get("group_id") or "")


def activity_frame_identity(
    contact_id: str,
    group_id: str,
    *,
    frame_key: str = "",
    fingerprint: str = "",
) -> tuple[str, int, bool]:
    """Return (frame_id, revision, duplicate).

    A provider item keeps the same frame while its accepted representation
    changes.  Exact retries keep the same revision so Glass can replay them
    idempotently.
    """
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        activity = contact.setdefault("activity", {})
        if activity.get("group_id") != group_id:
            activity.clear()
            activity.update(
                {
                    "run_id": group_id,
                    "group_id": group_id,
                    "sequence": 0,
                    "frames": {},
                    "settled": False,
                }
            )
        frames = activity.setdefault("frames", {})
        if frame_key:
            key = _stable_id("frame-key", frame_key)
        else:
            activity["sequence"] = int(activity.get("sequence") or 0) + 1
            key = f"sequence:{activity['sequence']}"
        frame = frames.get(key)
        duplicate = False
        if not isinstance(frame, dict):
            frame = {
                "frame_id": _stable_id("activity", group_id, key),
                "revision": 0,
                "fingerprint": fingerprint,
            }
            frames[key] = frame
        elif fingerprint and frame.get("fingerprint") == fingerprint:
            duplicate = True
        else:
            frame["revision"] = int(frame.get("revision") or 0) + 1
            frame["fingerprint"] = fingerprint
        if len(frames) > 500:
            for stale_key in list(frames)[: len(frames) - 500]:
                if stale_key != key:
                    frames.pop(stale_key, None)
        _write_state(state)
        return (
            str(frame["frame_id"]),
            int(frame.get("revision") or 0),
            duplicate,
        )


def settle_manager_activity(contact_id: str, group_id: str) -> None:
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        activity = contact.get("activity")
        if isinstance(activity, dict) and activity.get("group_id") == group_id:
            activity["settled"] = True
            _write_state(state)


def active_task_id(contact_id: str) -> str:
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        return str(contact.get("active_task_id") or "")


def set_active_task_timer(
    contact_id: str,
    *,
    timer_state: str,
    pause_reason: str | None = None,
    client: InterfaceClient | None = None,
) -> bool:
    """Best-effort external pause/resume using Glass-owned elapsed time."""
    task_id = active_task_id(contact_id)
    if not task_id:
        return False
    cached = _task_cache(contact_id, task_id)
    if cached.get("state") not in {"queued", "running"}:
        return False
    if timer_state == "paused" and pause_reason not in {
        "rate_limited",
        "offline",
        "infrastructure",
    }:
        return False
    if timer_state == "running" and cached.get("timer_state") != "paused":
        return False
    payload: dict[str, Any] = {"timer_state": timer_state}
    if timer_state == "paused":
        payload["timer_pause_reason"] = pause_reason
    else:
        payload["timer_pause_reason"] = None
    try:
        WorkUpdates(contact_id, client=client).execute(
            {
                "action": "task/update",
                "task_id": task_id,
                "data": payload,
            }
        )
    except Exception:
        return False
    return True


def _task_cache(contact_id: str, task_id: str) -> dict[str, Any]:
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        task = contact["tasks"].get(task_id)
        return deepcopy(task) if isinstance(task, dict) else {}


def _remember_task(contact_id: str, snapshot: dict[str, Any]) -> None:
    task_id = str(snapshot.get("task_id") or "")
    if not task_id:
        return
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
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
        _write_state(state)


def _remember_event(contact_id: str, snapshot: dict[str, Any]) -> None:
    task_id = str(snapshot.get("task_id") or "")
    if not task_id:
        return
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
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
        kind = str(snapshot.get("kind") or "")
        work_event_id = str(snapshot.get("work_event_id") or "")
        if work_event_id:
            task["events"][work_event_id] = deepcopy(snapshot)
        if kind == "worker_group" and snapshot.get("group_id"):
            task["worker_groups"][str(snapshot["group_id"])] = deepcopy(snapshot)
        if kind == "call" and snapshot.get("call_id"):
            task["calls"][str(snapshot["call_id"])] = deepcopy(snapshot)
        task["_cached_at"] = time.time()
        _write_state(state)


class WorkUpdates:
    """High-level durable update adapter for one contact manager."""

    def __init__(
        self,
        contact_id: str,
        *,
        client: InterfaceClient | None = None,
    ):
        self.contact_id = str(contact_id)
        self.client = client or InterfaceClient()
        contact = get_contact(self.contact_id)
        room_id = str((contact or {}).get("room_id") or "")
        if not room_id:
            raise WorkUpdateError(
                f"Contact '{self.contact_id}' has no Interface room."
            )
        self.contact = contact or {}
        self.room_id = room_id

    def _task_id(self, explicit: Any = "") -> str:
        task_id = str(explicit or active_task_id(self.contact_id) or "")
        if not task_id:
            raise WorkUpdateError(
                "No active durable task. Create one with task/create first."
            )
        return task_id

    def _refresh_task(self, task_id: str) -> dict[str, Any]:
        try:
            snapshot = _result_data(self.client.work_task_show(task_id))
        except Exception:
            return _task_cache(self.contact_id, task_id)
        _remember_task(self.contact_id, snapshot)
        return snapshot

    def _task_revision(self, task_id: str) -> int | None:
        cached = _task_cache(self.contact_id, task_id)
        value = cached.get("revision")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def execute(self, spec: dict[str, Any]) -> Any:
        action = str(spec.get("action") or spec.get("type") or "").strip().lower()
        data = spec.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise WorkUpdateError("work_update data must be an object.")
        payload = deepcopy(data)

        if action == "task/create":
            return self._task_create(payload)
        if action == "task/update":
            return self._task_update(spec, payload)
        if action == "todo/add":
            return self._todo_add(spec, payload)
        if action == "todo/update":
            return self._todo_update(spec, payload)
        if action == "milestone":
            return self._milestone(spec, payload)
        if action == "blocker/create":
            return self._blocker_create(spec, payload)
        if action == "blocker/resolve":
            return self._blocker_resolve(spec, payload)
        if action == "worker-group/create":
            return self._worker_group_create(spec, payload)
        if action == "worker-group/update":
            return self._worker_group_update(spec, payload)
        if action == "worker/create":
            return self._worker_create(spec, payload)
        if action == "worker/update":
            return self._worker_update(spec, payload)
        if action == "call/create":
            return self._call_create(spec, payload)
        if action == "call/update":
            return self._call_update(spec, payload)
        if action in TERMINAL_ACTIONS:
            return self._terminal(action, spec, payload)
        raise WorkUpdateError(f"Unknown work_update action '{action}'.")

    def _task_create(self, payload: dict[str, Any]) -> Any:
        task_id = str(payload.get("task_id") or _new_id("task", payload.get("title")))
        payload["task_id"] = task_id
        payload["room_id"] = self.room_id
        payload.setdefault("schema_version", 1)
        payload.setdefault("state", "running")
        todos = payload.get("todos")
        if isinstance(todos, list):
            for todo in todos:
                if isinstance(todo, dict):
                    todo.setdefault("todo_id", _new_id("todo", todo.get("title")))
        payload.setdefault("client_id", _stable_id("create-task", task_id))
        result = self.client.work_task_create(payload)
        snapshot = _result_data(result)
        _remember_task(self.contact_id, snapshot or payload)
        return result

    def _task_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id") or payload.pop("task_id", ""))
        if "revision" not in payload:
            revision = self._task_revision(task_id)
            if revision is not None:
                payload["revision"] = revision
        result = self.client.work_task_patch(task_id, payload)
        _remember_task(self.contact_id, _result_data(result))
        return result

    def _todo_add(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        todo_id = str(payload.get("todo_id") or _new_id("todo", payload.get("title")))
        payload["todo_id"] = todo_id
        payload.setdefault("client_id", _stable_id("create-todo", task_id, todo_id))
        result = self.client.work_todo_add(task_id, payload)
        _remember_task(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _todo_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        todo_id = str(spec.get("todo_id") or payload.pop("todo_id", ""))
        if not todo_id:
            raise WorkUpdateError("todo/update requires todo_id.")
        if "revision" not in payload:
            cached = _task_cache(self.contact_id, task_id)
            todo = (cached.get("todos") or {}).get(todo_id, {})
            revision = todo.get("revision") if isinstance(todo, dict) else None
            if isinstance(revision, int) and not isinstance(revision, bool):
                payload["revision"] = revision
        result = self.client.work_todo_patch(task_id, todo_id, payload)
        _remember_task(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _milestone(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        event_id = str(
            payload.get("work_event_id")
            or _new_id("milestone", payload.get("body"))
        )
        payload["work_event_id"] = event_id
        payload.setdefault("kind", "milestone")
        payload.setdefault("blocks", [])
        payload.setdefault("client_id", _stable_id("milestone", task_id, event_id))
        result = self.client.work_milestone_create(task_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _blocker_create(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        blocker_id = str(
            payload.get("blocker_id")
            or _new_id("blocker", payload.get("body"))
        )
        event_id = str(
            payload.get("work_event_id")
            or _stable_id("blocker-event", task_id, blocker_id)
        )
        payload.update(
            {
                "work_event_id": event_id,
                "blocker_id": blocker_id,
                "kind": "blocker",
            }
        )
        payload.setdefault("state", "open")
        payload.setdefault("resolved_at", None)
        payload.setdefault("blocks", [])
        payload.setdefault(
            "client_id",
            _stable_id("create-blocker", task_id, blocker_id),
        )
        result = self.client.work_blocker_create(task_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _blocker_resolve(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        blocker_id = str(spec.get("blocker_id") or payload.pop("blocker_id", ""))
        if not blocker_id:
            raise WorkUpdateError("blocker/resolve requires blocker_id.")
        if "revision" not in payload:
            cached = _task_cache(self.contact_id, task_id)
            for event in (cached.get("events") or {}).values():
                if (
                    isinstance(event, dict)
                    and event.get("kind") == "blocker"
                    and event.get("blocker_id") == blocker_id
                ):
                    revision = event.get("revision")
                    if isinstance(revision, int) and not isinstance(revision, bool):
                        payload["revision"] = revision
                    break
        payload.setdefault("state", "resolved")
        payload.setdefault("blocks", [])
        payload.setdefault(
            "client_id",
            _stable_id("resolve-blocker", task_id, blocker_id),
        )
        result = self.client.work_blocker_resolve(task_id, blocker_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_group_create(
        self,
        spec: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(
            payload.get("group_id")
            or _new_id("worker-group", payload.get("body"))
        )
        event_id = str(
            payload.get("work_event_id")
            or _stable_id("worker-group-event", task_id, group_id)
        )
        payload.update(
            {
                "group_id": group_id,
                "work_event_id": event_id,
                "kind": "worker_group",
            }
        )
        payload.setdefault("body", "Started workers")
        payload.setdefault("blocks", [])
        payload.setdefault("workers", [])
        payload.setdefault(
            "client_id",
            _stable_id("create-worker-group", task_id, group_id),
        )
        result = self.client.work_worker_group_create(task_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_group_update(
        self,
        spec: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(spec.get("group_id") or payload.pop("group_id", ""))
        if not group_id:
            raise WorkUpdateError("worker-group/update requires group_id.")
        if "revision" not in payload:
            cached = _task_cache(self.contact_id, task_id)
            group = (cached.get("worker_groups") or {}).get(group_id, {})
            revision = group.get("revision") if isinstance(group, dict) else None
            if isinstance(revision, int) and not isinstance(revision, bool):
                payload["revision"] = revision
        result = self.client.work_worker_group_patch(task_id, group_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_create(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(spec.get("group_id") or "")
        if not group_id:
            raise WorkUpdateError("worker/create requires group_id.")
        worker_id = str(payload.get("worker_id") or _new_id("worker"))
        invocation_id = str(
            payload.get("invocation_id")
            or _new_id("invocation", worker_id)
        )
        payload["worker_id"] = worker_id
        payload["invocation_id"] = invocation_id
        payload.setdefault("state", "in_progress")
        payload.setdefault("history", [])
        payload.setdefault(
            "client_id",
            _stable_id("create-worker", task_id, group_id, invocation_id),
        )
        result = self.client.work_worker_create(task_id, group_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(spec.get("group_id") or "")
        invocation_id = str(spec.get("invocation_id") or "")
        if not group_id or not invocation_id:
            raise WorkUpdateError(
                "worker/update requires group_id and invocation_id."
            )
        if payload.get("state") and payload["state"] not in WORKER_STATES:
            raise WorkUpdateError("worker/update state is invalid.")
        if "revision" not in payload:
            cached = _task_cache(self.contact_id, task_id)
            group = (cached.get("worker_groups") or {}).get(group_id, {})
            workers = group.get("workers") if isinstance(group, dict) else []
            for worker in workers or []:
                if (
                    isinstance(worker, dict)
                    and worker.get("invocation_id") == invocation_id
                ):
                    revision = worker.get("revision")
                    if isinstance(revision, int) and not isinstance(revision, bool):
                        payload["revision"] = revision
                    break
        result = self.client.work_worker_patch(
            task_id,
            group_id,
            invocation_id,
            payload,
        )
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _call_create(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        call_id = str(payload.get("call_id") or _new_id("call", payload.get("target_id")))
        event_id = str(
            payload.get("work_event_id")
            or _stable_id("call-event", task_id, call_id)
        )
        payload.update(
            {
                "call_id": call_id,
                "work_event_id": event_id,
                "kind": "call",
            }
        )
        payload.setdefault("state", "connecting")
        payload.setdefault("body", "")
        payload.setdefault("blocks", [])
        payload.setdefault("transcript", [])
        payload.setdefault("client_id", _stable_id("create-call", task_id, call_id))
        result = self.client.work_call_create(task_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        return result

    def _call_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        call_id = str(spec.get("call_id") or payload.pop("call_id", ""))
        if not call_id:
            raise WorkUpdateError("call/update requires call_id.")
        if payload.get("state") and payload["state"] not in CALL_STATES:
            raise WorkUpdateError("call/update state is invalid.")
        if "revision" not in payload:
            cached = _task_cache(self.contact_id, task_id)
            call = (cached.get("calls") or {}).get(call_id, {})
            revision = call.get("revision") if isinstance(call, dict) else None
            if isinstance(revision, int) and not isinstance(revision, bool):
                payload["revision"] = revision
        result = self.client.work_call_patch(task_id, call_id, payload)
        _remember_event(self.contact_id, _result_data(result))
        self._refresh_task(task_id)
        if payload.get("state") in {"completed", "failed", "cancelled"}:
            _clear_call_correlations(call_id)
        return result

    def _terminal(
        self,
        action: str,
        spec: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        transition, kind, _ = TERMINAL_ACTIONS[action]
        event_id = str(
            payload.get("work_event_id")
            or _stable_id("terminal-event", task_id, transition)
        )
        payload["work_event_id"] = event_id
        payload.setdefault("kind", kind)
        payload.setdefault("body", "")
        payload.setdefault("blocks", [])
        payload.setdefault(
            "client_id",
            _stable_id(f"task-{transition}", task_id),
        )
        result = self.client.work_task_transition(task_id, transition, payload)
        _remember_event(self.contact_id, _result_data(result))
        snapshot = self._refresh_task(task_id)
        if not snapshot:
            with _state_guard():
                state = _read_state()
                contact = _contact_state(state, self.contact_id)
                task = contact["tasks"].setdefault(task_id, {})
                task["state"] = TERMINAL_ACTIONS[action][2]
                if contact.get("active_task_id") == task_id:
                    contact["active_task_id"] = ""
                _write_state(state)
        return result


def execute_work_update(
    tool_spec: dict[str, Any],
    contact_id: str,
    *,
    client: InterfaceClient | None = None,
) -> str:
    """Execute one manager work_update tool and return a manager-loop result."""
    action = str(tool_spec.get("action") or tool_spec.get("type") or "")
    try:
        result = WorkUpdates(contact_id, client=client).execute(tool_spec)
    except (WorkUpdateError, InterfaceError, OSError, ValueError) as exc:
        return f"Error: work_update {action or 'unknown'} failed: {exc}"
    except Exception as exc:
        return f"Error: work_update {action or 'unknown'} failed unexpectedly: {exc}"
    return f"Done. work_update {action}: {_public_result(result)}"


def _clear_call_correlations(call_id: str) -> None:
    with _state_guard():
        state = _read_state()
        changed = False
        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if not isinstance(pending, dict):
                continue
            for peer_id, correlation in list(pending.items()):
                if not isinstance(correlation, dict):
                    continue
                if call_id in {
                    correlation.get("outbound_call_id"),
                    correlation.get("inbound_call_id"),
                }:
                    pending.pop(peer_id, None)
                    changed = True
        if changed:
            _write_state(state)


def _discard_call_reference(call_id: str, prefix: str) -> None:
    """Drop one failed remote card while preserving its valid peer card."""
    prefix = "inbound" if prefix == "inbound" else "outbound"
    with _state_guard():
        state = _read_state()
        changed = False
        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if not isinstance(pending, dict):
                continue
            for peer_id, correlation in list(pending.items()):
                if (
                    not isinstance(correlation, dict)
                    or correlation.get(f"{prefix}_call_id") != call_id
                ):
                    continue
                for field in ("owner_contact_id", "task_id", "call_id"):
                    correlation[f"{prefix}_{field}"] = ""
                correlation["updated_at"] = time.time()
                if not (
                    _has_call_reference(correlation, "outbound")
                    or _has_call_reference(correlation, "inbound")
                ):
                    pending.pop(peer_id, None)
                changed = True
        if changed:
            _write_state(state)


def record_worker_started(
    contact_id: str,
    worker_id: str,
    worker_type: str,
    description: str,
    *,
    queued: bool = False,
    task_id: str = "",
    client: InterfaceClient | None = None,
) -> dict[str, str]:
    """Best-effort bridge from the real worker lifecycle to a durable card."""
    task_id = str(task_id or active_task_id(contact_id) or "")
    if not task_id:
        return {}
    group_id = _stable_id("worker-group", contact_id, task_id)
    invocation_id = _new_id("invocation", worker_id)
    try:
        updates = WorkUpdates(contact_id, client=client)
    except Exception:
        return {}
    cached = _task_cache(contact_id, task_id)
    if group_id not in (cached.get("worker_groups") or {}):
        try:
            updates.execute(
                {
                    "action": "worker-group/create",
                    "task_id": task_id,
                    "data": {
                        "group_id": group_id,
                        "work_event_id": _stable_id(
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
                    "name": _compact(description, 500) or worker_id,
                    "description": (
                        "Queued and waiting to launch"
                        if queued
                        else f"{worker_type.capitalize()} worker is running"
                    ),
                    "state": "yet_to_start" if queued else "in_progress",
                    "history": [],
                },
            }
        )
    except Exception:
        return {}
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        contact["workers"][worker_id] = {
            "task_id": task_id,
            "group_id": group_id,
            "invocation_id": invocation_id,
            "state": "yet_to_start" if queued else "in_progress",
        }
        _write_state(state)
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
    if state_name not in WORKER_STATES:
        return False
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        correlation = deepcopy(contact.get("workers", {}).get(worker_id) or {})
    if not correlation:
        return False
    try:
        WorkUpdates(contact_id, client=client).execute(
            {
                "action": "worker/update",
                "task_id": correlation["task_id"],
                "group_id": correlation["group_id"],
                "invocation_id": correlation["invocation_id"],
                "data": {
                    "state": state_name,
                    "description": _compact(description, 500),
                },
            }
        )
    except Exception:
        return False
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        current = contact.get("workers", {}).get(worker_id)
        if isinstance(current, dict):
            if state_name in {"completed", "failed", "cancelled"}:
                contact.get("workers", {}).pop(worker_id, None)
            else:
                current["state"] = state_name
            _write_state(state)
    return True


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
    task_id = str(task_id or active_task_id(contact_id) or "")
    target_kind = "silicon" if target_kind == "silicon" else "manager"
    with _state_guard():
        state = _read_state()
        contact_state = _contact_state(state, contact_id)
        pending = deepcopy(
            contact_state.get("pending_calls", {}).get(str(target_id)) or {}
        )
    if _has_call_reference(pending, "outbound") or _has_call_reference(
        pending,
        "inbound",
    ):
        local_reference = _call_reference_for_owner(pending, contact_id)
        return {
            **local_reference,
            "target_kind": target_kind,
            "target_id": target_id,
            "continuation": True,
        }
    if not task_id:
        return {
            "owner_contact_id": contact_id,
            "task_id": "",
            "call_id": "",
            "target_kind": target_kind,
            "target_id": target_id,
            "unattached": True,
        }
    return {
        "owner_contact_id": contact_id,
        "task_id": task_id,
        "call_id": _new_id("call", target_id),
        "target_kind": target_kind,
        "target_id": target_id,
    }


def _persist_outbound_call(
    reference: dict[str, Any],
    *,
    target_name: str,
    message: str,
    client: InterfaceClient | None = None,
) -> bool:
    contact_id = str(reference.get("owner_contact_id") or "")
    target_id = str(reference.get("target_id") or "")
    if reference.get("unattached"):
        return True
    if reference.get("continuation"):
        with _state_guard():
            state = _read_state()
            contact_state = _contact_state(state, contact_id)
            correlation = deepcopy(
                contact_state.get("pending_calls", {}).get(target_id) or {}
            )
        if not correlation:
            return False
        _append_correlated_call(
            correlation,
            speaker_kind="manager",
            speaker_id=f"manager:{contact_id}",
            speaker_name=str(get_own_profile().get("name") or "Silicon manager"),
            message=message,
            client=client,
        )
        return True

    task_id = str(reference.get("task_id") or "")
    call_id = str(reference.get("call_id") or "")
    target_kind = str(reference.get("target_kind") or "manager")
    if not contact_id or not task_id or not call_id:
        return False
    self_name = str(get_own_profile().get("name") or "Silicon manager")
    transcript_id = _new_id("transcript", "outbound")
    try:
        WorkUpdates(contact_id, client=client).execute(
            {
                "action": "call/create",
                "task_id": task_id,
                "data": {
                    "call_id": call_id,
                    "work_event_id": _stable_id(
                        "call-event",
                        contact_id,
                        task_id,
                        call_id,
                    ),
                    "direction": "outbound",
                    "target_kind": target_kind,
                    "target_id": _safe_fragment(target_id, "unknown"),
                    "target_name": target_name or target_id,
                    "state": "in_progress",
                    "body": (
                        f"Called {target_name or target_id}"
                        if target_kind == "silicon"
                        else f"Calling {target_name or target_id}"
                    ),
                    "blocks": [],
                    "transcript": [
                        {
                            "transcript_id": transcript_id,
                            "speaker_kind": "manager",
                            "speaker_id": _safe_fragment(
                                f"manager:{contact_id}",
                                "manager",
                            ),
                            "speaker_name": self_name or contact_id,
                            "body": message,
                            "blocks": [],
                            "revision": 0,
                        }
                    ],
                },
            }
        )
    except Exception:
        _discard_call_reference(call_id, "outbound")
        return False
    return True


def enqueue_outbound_call(
    reference: dict[str, Any],
    *,
    target_name: str,
    message: str,
    client: InterfaceClient | None = None,
) -> bool:
    """Persist a prepared call card after primary delivery has been accepted."""
    owner = str(reference.get("owner_contact_id") or "")
    return submit_best_effort(
        _persist_outbound_call,
        dict(reference),
        target_name=target_name,
        message=message,
        client=client,
        key=f"work-owner:{owner}",
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
    if not _persist_outbound_call(
        reference,
        target_name=target_name,
        message=message,
        client=client,
    ):
        return {}
    return reference


def _append_call_entry(
    owner_contact_id: str,
    task_id: str,
    call_id: str,
    *,
    speaker_kind: str,
    speaker_id: str,
    speaker_name: str,
    message: str,
    client: InterfaceClient | None = None,
) -> bool:
    if not owner_contact_id or not task_id or not call_id:
        return False
    try:
        WorkUpdates(owner_contact_id, client=client).execute(
            {
                "action": "call/update",
                "task_id": task_id,
                "call_id": call_id,
                "data": {
                    "state": "in_progress",
                    "transcript": [
                        {
                            "transcript_id": _new_id("transcript", "message"),
                            "speaker_kind": (
                                "silicon" if speaker_kind == "silicon" else "manager"
                            ),
                            "speaker_id": _safe_fragment(speaker_id, "speaker"),
                            "speaker_name": speaker_name or speaker_id,
                            "body": message,
                            "blocks": [],
                            "revision": 0,
                        }
                    ],
                },
            }
        )
    except Exception:
        return False
    return True


def _append_correlated_call(
    correlation: dict[str, Any],
    *,
    speaker_kind: str,
    speaker_id: str,
    speaker_name: str,
    message: str,
    client: InterfaceClient | None = None,
) -> None:
    _append_call_entry(
        str(correlation.get("outbound_owner_contact_id") or ""),
        str(correlation.get("outbound_task_id") or ""),
        str(correlation.get("outbound_call_id") or ""),
        speaker_kind=speaker_kind,
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        message=message,
        client=client,
    )
    _append_call_entry(
        str(correlation.get("inbound_owner_contact_id") or ""),
        str(correlation.get("inbound_task_id") or ""),
        str(correlation.get("inbound_call_id") or ""),
        speaker_kind=speaker_kind,
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        message=message,
        client=client,
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
    """Allocate and cache both sides of a correlation without network I/O."""
    outbound = dict(outbound or {})
    task_id = active_task_id(contact_id)
    call_id = _new_id("call", source_id) if task_id else ""
    correlation = {
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
        "inbound_owner_contact_id": contact_id if call_id else "",
        "inbound_task_id": task_id if call_id else "",
        "inbound_call_id": call_id,
        "source_kind": source_kind,
        "source_id": source_id,
        "updated_at": time.time(),
    }
    if _has_call_reference(correlation, "outbound") or _has_call_reference(
        correlation,
        "inbound",
    ):
        with _state_guard():
            state = _read_state()
            recipient = _contact_state(state, contact_id)
            recipient["pending_calls"][source_id] = deepcopy(correlation)
            outbound_owner = correlation["outbound_owner_contact_id"]
            if outbound_owner:
                owner = _contact_state(state, outbound_owner)
                owner["pending_calls"][contact_id] = deepcopy(correlation)
            _write_state(state)
    reference = {
        "owner_contact_id": contact_id if call_id else "",
        "task_id": task_id if call_id else "",
        "call_id": call_id,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_name": source_name,
        "message": message,
    }
    return reference


def _persist_inbound_call(
    reference: dict[str, Any],
    *,
    client: InterfaceClient | None = None,
) -> bool:
    contact_id = str(reference.get("owner_contact_id") or "")
    task_id = str(reference.get("task_id") or "")
    call_id = str(reference.get("call_id") or "")
    if not contact_id or not task_id or not call_id:
        return True
    source_kind = str(reference.get("source_kind") or "")
    source_id = str(reference.get("source_id") or "")
    source_name = str(reference.get("source_name") or "")
    message = str(reference.get("message") or "")
    speaker_kind = "silicon" if source_kind == "silicon" else "manager"
    try:
        WorkUpdates(contact_id, client=client).execute(
            {
                "action": "call/create",
                "task_id": task_id,
                "data": {
                    "call_id": call_id,
                    "work_event_id": _stable_id(
                        "call-event",
                        contact_id,
                        task_id,
                        call_id,
                    ),
                    "direction": "inbound",
                    "target_kind": speaker_kind,
                    "target_id": _safe_fragment(source_id, "unknown"),
                    "target_name": source_name or source_id,
                    "state": "in_progress",
                    "body": f"Received call from {source_name or source_id}",
                    "blocks": [],
                    "transcript": [
                        {
                            "transcript_id": _new_id("transcript", "inbound"),
                            "speaker_kind": speaker_kind,
                            "speaker_id": _safe_fragment(
                                (
                                    source_id
                                    if speaker_kind == "silicon"
                                    else f"manager:{source_id}"
                                ),
                                "speaker",
                            ),
                            "speaker_name": source_name or source_id,
                            "body": message,
                            "blocks": [],
                            "revision": 0,
                        }
                    ],
                },
            }
        )
    except Exception:
        _discard_call_reference(call_id, "inbound")
        return False
    return True


def enqueue_inbound_call(
    contact_id: str,
    *,
    source_kind: str,
    source_id: str,
    source_name: str,
    message: str,
    outbound: dict[str, Any] | None = None,
    client: InterfaceClient | None = None,
) -> dict[str, str]:
    reference = prepare_inbound_call(
        contact_id,
        source_kind=source_kind,
        source_id=source_id,
        source_name=source_name,
        message=message,
        outbound=outbound,
    )
    submit_best_effort(
        _persist_inbound_call,
        reference,
        client=client,
        key=f"work-owner:{contact_id}",
    )
    return {
        "owner_contact_id": str(reference.get("owner_contact_id") or ""),
        "task_id": str(reference.get("task_id") or ""),
        "call_id": str(reference.get("call_id") or ""),
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
    if not _persist_inbound_call(reference, client=client):
        return {"owner_contact_id": "", "task_id": "", "call_id": ""}
    return {
        "owner_contact_id": str(reference.get("owner_contact_id") or ""),
        "task_id": str(reference.get("task_id") or ""),
        "call_id": str(reference.get("call_id") or ""),
    }


def record_contact_call_message(
    contact_id: str,
    *,
    speaker_kind: str,
    speaker_id: str,
    speaker_name: str,
    message: str,
    client: InterfaceClient | None = None,
) -> bool:
    """Append a real wire message to the most recent correlated call."""
    with _state_guard():
        state = _read_state()
        contact = _contact_state(state, contact_id)
        candidates = [
            value
            for value in contact.get("pending_calls", {}).values()
            if isinstance(value, dict)
            and (
                _has_call_reference(value, "outbound")
                or _has_call_reference(value, "inbound")
            )
        ]
    if not candidates:
        return False
    correlation = max(
        candidates,
        key=lambda value: _timestamp(value.get("updated_at")),
    )
    _append_correlated_call(
        correlation,
        speaker_kind=speaker_kind,
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        message=message,
        client=client,
    )
    with _state_guard():
        state = _read_state()
        changed = False
        for contact in state.get("contacts", {}).values():
            if not isinstance(contact, dict):
                continue
            pending = contact.get("pending_calls")
            if not isinstance(pending, dict):
                continue
            for value in pending.values():
                if (
                    isinstance(value, dict)
                    and value.get("outbound_call_id")
                    == correlation.get("outbound_call_id")
                    and value.get("inbound_call_id")
                    == correlation.get("inbound_call_id")
                ):
                    value["updated_at"] = time.time()
                    changed = True
        if changed:
            _write_state(state)
    return True
