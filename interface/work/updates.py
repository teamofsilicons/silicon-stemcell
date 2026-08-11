"""The work_update tool: one action, dispatched.

:class:`WorkUpdates` is the surface a manager reaches through — every action
it can take on a card, in one place, so a new action is a new method rather
than a new branch somewhere else.
"""
from __future__ import annotations
from interface import state as interface_state

from interface.work import constants
from interface.work import cache as cache_module
from interface.work import correlation as correlation_module
from interface.work import delivery as delivery_module
from interface.work import identity as identity_module
from interface.work import journal as journal_module
from interface.work import store as store_module
from copy import deepcopy
from typing import Any
from interface import (
    InterfaceClient,
    InterfaceError,

)


_ACTION_HANDLERS = {
    "task/update": "_task_update",
    "todo/add": "_todo_add",
    "todo/update": "_todo_update",
    "milestone": "_milestone",
    "blocker/create": "_blocker_create",
    "blocker/resolve": "_blocker_resolve",
    "worker-group/create": "_worker_group_create",
    "worker-group/update": "_worker_group_update",
    "worker/create": "_worker_create",
    "worker/update": "_worker_update",
    "call/create": "_call_create",
    "call/update": "_call_update",
}


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
        contact = interface_state.get_contact(self.contact_id)
        room_id = str((contact or {}).get("room_id") or "")
        if not room_id:
            raise constants.WorkUpdateError(
                f"Contact '{self.contact_id}' has no Interface room."
            )
        self.contact = contact or {}
        self.room_id = room_id

    def _task_id(self, explicit: Any = "") -> str:
        task_id = str(explicit or cache_module.active_task_id(self.contact_id) or "")
        if not task_id:
            raise constants.WorkUpdateError(
                "No active durable task. Create one with task/create first."
            )
        return task_id

    def _refresh_task(self, task_id: str) -> dict[str, Any]:
        try:
            snapshot = identity_module._result_data(self.client.work_task_show(task_id))
        except Exception:
            return cache_module._task_cache(self.contact_id, task_id)
        cache_module._remember_task(self.contact_id, snapshot)
        return snapshot

    def _task_revision(self, task_id: str) -> int | None:
        cached = cache_module._task_cache(self.contact_id, task_id)
        value = cached.get("revision")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def execute(self, spec: dict[str, Any]) -> Any:
        action = str(spec.get("action") or spec.get("type") or "").strip().lower()
        data = spec.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise constants.WorkUpdateError("work_update data must be an object.")
        payload = deepcopy(data)

        if action == "task/create":
            return self._task_create(payload)
        handler = _ACTION_HANDLERS.get(action)
        if handler is not None:
            return getattr(self, handler)(spec, payload)
        if action in constants.TERMINAL_ACTIONS:
            return self._terminal(action, spec, payload)
        raise constants.WorkUpdateError(f"Unknown work_update action '{action}'.")

    def _task_create(self, payload: dict[str, Any]) -> Any:
        task_id = str(payload.get("task_id") or identity_module._new_id("task", payload.get("title")))
        payload["task_id"] = task_id
        payload["room_id"] = self.room_id
        payload.setdefault("schema_version", 1)
        payload.setdefault("state", "running")
        todos = payload.get("todos")
        if isinstance(todos, list):
            for todo in todos:
                if isinstance(todo, dict):
                    todo.setdefault("todo_id", identity_module._new_id("todo", todo.get("title")))
        payload.setdefault("client_id", identity_module._stable_id("create-task", task_id))
        result = self.client.work_task_create(payload)
        snapshot = identity_module._result_data(result)
        cache_module._remember_task(self.contact_id, snapshot or payload)
        return result

    def _task_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id") or payload.pop("task_id", ""))
        if "revision" not in payload:
            revision = self._task_revision(task_id)
            if revision is not None:
                payload["revision"] = revision
        result = self.client.work_task_patch(task_id, payload)
        cache_module._remember_task(self.contact_id, identity_module._result_data(result))
        return result

    def _todo_add(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        todo_id = str(payload.get("todo_id") or identity_module._new_id("todo", payload.get("title")))
        payload["todo_id"] = todo_id
        payload.setdefault("client_id", identity_module._stable_id("create-todo", task_id, todo_id))
        result = self.client.work_todo_add(task_id, payload)
        cache_module._remember_task(self.contact_id, identity_module._result_data(result))
        self._refresh_task(task_id)
        return result

    def _todo_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        todo_id = str(spec.get("todo_id") or payload.pop("todo_id", ""))
        if not todo_id:
            raise constants.WorkUpdateError("todo/update requires todo_id.")
        if "revision" not in payload:
            cached = cache_module._task_cache(self.contact_id, task_id)
            todo = (cached.get("todos") or {}).get(todo_id, {})
            revision = todo.get("revision") if isinstance(todo, dict) else None
            if isinstance(revision, int) and not isinstance(revision, bool):
                payload["revision"] = revision
        result = self.client.work_todo_patch(task_id, todo_id, payload)
        cache_module._remember_task(self.contact_id, identity_module._result_data(result))
        self._refresh_task(task_id)
        return result

    def _milestone(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        event_id = str(
            payload.get("work_event_id")
            or identity_module._new_id("milestone", payload.get("body"))
        )
        payload["work_event_id"] = event_id
        payload.setdefault("kind", "milestone")
        payload.setdefault("blocks", [])
        payload.setdefault("client_id", identity_module._stable_id("milestone", task_id, event_id))
        result = self.client.work_milestone_create(task_id, payload)
        cache_module._remember_event(self.contact_id, identity_module._result_data(result))
        self._refresh_task(task_id)
        return result

    def _blocker_create(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        blocker_id = str(
            payload.get("blocker_id")
            or identity_module._new_id("blocker", payload.get("body"))
        )
        event_id = str(
            payload.get("work_event_id")
            or identity_module._stable_id("blocker-event", task_id, blocker_id)
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
            identity_module._stable_id("create-blocker", task_id, blocker_id),
        )
        result = self.client.work_blocker_create(task_id, payload)
        cache_module._remember_event(self.contact_id, identity_module._result_data(result))
        self._refresh_task(task_id)
        return result

    def _blocker_resolve(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        blocker_id = str(spec.get("blocker_id") or payload.pop("blocker_id", ""))
        if not blocker_id:
            raise constants.WorkUpdateError("blocker/resolve requires blocker_id.")
        if "revision" not in payload:
            cached = cache_module._task_cache(self.contact_id, task_id)
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
            identity_module._stable_id("resolve-blocker", task_id, blocker_id),
        )
        result = self.client.work_blocker_resolve(task_id, blocker_id, payload)
        cache_module._remember_event(self.contact_id, identity_module._result_data(result))
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
            or identity_module._new_id("worker-group", payload.get("body"))
        )
        event_id = str(
            payload.get("work_event_id")
            or identity_module._stable_id("worker-group-event", task_id, group_id)
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
            identity_module._stable_id("create-worker-group", task_id, group_id),
        )
        result = self.client.work_worker_group_create(task_id, payload)
        cache_module._remember_event(self.contact_id, identity_module._result_data(result))
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
            raise constants.WorkUpdateError("worker-group/update requires group_id.")
        if "revision" not in payload:
            cached = cache_module._task_cache(self.contact_id, task_id)
            group = (cached.get("worker_groups") or {}).get(group_id, {})
            revision = group.get("revision") if isinstance(group, dict) else None
            if isinstance(revision, int) and not isinstance(revision, bool):
                payload["revision"] = revision
        result = self.client.work_worker_group_patch(task_id, group_id, payload)
        cache_module._remember_event(self.contact_id, identity_module._result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_create(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(spec.get("group_id") or "")
        if not group_id:
            raise constants.WorkUpdateError("worker/create requires group_id.")
        worker_id = str(payload.get("worker_id") or identity_module._new_id("worker"))
        invocation_id = str(
            payload.get("invocation_id")
            or identity_module._new_id("invocation", worker_id)
        )
        payload["worker_id"] = worker_id
        payload["invocation_id"] = invocation_id
        payload.setdefault("state", "in_progress")
        payload.setdefault("history", [])
        payload.setdefault(
            "client_id",
            identity_module._stable_id("create-worker", task_id, group_id, invocation_id),
        )
        result = self.client.work_worker_create(task_id, group_id, payload)
        cache_module._remember_event(self.contact_id, identity_module._result_data(result))
        self._refresh_task(task_id)
        return result

    def _worker_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        group_id = str(spec.get("group_id") or "")
        invocation_id = str(spec.get("invocation_id") or "")
        if not group_id or not invocation_id:
            raise constants.WorkUpdateError(
                "worker/update requires group_id and invocation_id."
            )
        if payload.get("state") and payload["state"] not in constants.WORKER_STATES:
            raise constants.WorkUpdateError("worker/update state is invalid.")
        if "revision" not in payload:
            cached = cache_module._task_cache(self.contact_id, task_id)
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
        cache_module._remember_event(self.contact_id, identity_module._result_data(result))
        self._refresh_task(task_id)
        return result

    def _call_create(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        force_standalone = bool(spec.get("standalone"))
        task_id = (
            ""
            if force_standalone
            else str(spec.get("task_id") or cache_module.active_task_id(self.contact_id) or "")
        )
        call_id = str(payload.get("call_id") or identity_module._new_id("call", payload.get("target_id")))
        event_id = str(
            payload.get("work_event_id")
            or identity_module._stable_id("call-event", task_id or self.room_id, call_id)
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
        payload.setdefault(
            "client_id",
            identity_module._stable_id("create-call", task_id or self.room_id, call_id),
        )
        if task_id:
            result = self.client.work_call_create(task_id, payload)
        else:
            payload["room_id"] = self.room_id
            result = self.client.work_standalone_call_create(payload)
        cache_module._remember_event(self.contact_id, identity_module._result_data(result))
        if task_id:
            self._refresh_task(task_id)
        return result

    def _call_update(self, spec: dict[str, Any], payload: dict[str, Any]) -> Any:
        call_id = str(spec.get("call_id") or payload.pop("call_id", ""))
        if not call_id:
            raise constants.WorkUpdateError("call/update requires call_id.")
        explicit_task_id = str(
            spec.get("task_id") or payload.pop("task_id", "") or ""
        )
        cached_standalone = cache_module._standalone_call_cache(self.contact_id, call_id)
        # Omitted task_id is the standalone route by contract. Never infer an
        # unrelated active task after local cache/correlation loss.
        task_id = "" if spec.get("standalone") else explicit_task_id
        if payload.get("state") and payload["state"] not in constants.CALL_STATES:
            raise constants.WorkUpdateError("call/update state is invalid.")
        reference = {
            "owner_contact_id": self.contact_id,
            "task_id": task_id,
            "call_id": call_id,
            "work_event_id": str(
                (
                    (
                        (cache_module._task_cache(self.contact_id, task_id).get("calls") or {})
                        .get(call_id, {})
                    )
                    if task_id
                    else cached_standalone
                ).get("work_event_id")
                or ""
            ),
        }
        if payload.get("state") in {"completed", "failed", "cancelled"}:
            correlation_module._mark_call_correlations_terminal(call_id)
        retry_id = journal_module._journal_call_patch(reference, payload)
        result = delivery_module._deliver_call_retry(retry_id, client=self.client)
        if isinstance(result, dict):
            return result
        return {
            "call_id": call_id,
            "task_id": task_id or None,
            "state": payload.get(
                "state",
                cached_standalone.get("state") or "in_progress",
            ),
            "queued_for_delivery": True,
        }

    def _terminal(
        self,
        action: str,
        spec: dict[str, Any],
        payload: dict[str, Any],
    ) -> Any:
        task_id = self._task_id(spec.get("task_id"))
        transition, kind, _ = constants.TERMINAL_ACTIONS[action]
        event_id = str(
            payload.get("work_event_id")
            or identity_module._stable_id("terminal-event", task_id, transition)
        )
        payload["work_event_id"] = event_id
        payload.setdefault("kind", kind)
        payload.setdefault("body", "")
        payload.setdefault("blocks", [])
        payload.setdefault(
            "client_id",
            identity_module._stable_id(f"task-{transition}", task_id),
        )
        result = self.client.work_task_transition(task_id, transition, payload)
        cache_module._remember_event(self.contact_id, identity_module._result_data(result))
        snapshot = self._refresh_task(task_id)
        if not snapshot:
            with store_module._state_guard():
                state = store_module._read_state()
                contact = store_module._contact_state(state, self.contact_id)
                task = contact["tasks"].setdefault(task_id, {})
                task["state"] = constants.TERMINAL_ACTIONS[action][2]
                if contact.get("cache_module.active_task_id") == task_id:
                    contact["cache_module.active_task_id"] = ""
                store_module._write_state(state)
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
    except (constants.WorkUpdateError, InterfaceError, OSError, ValueError) as exc:
        return f"Error: work_update {action or 'unknown'} failed: {exc}"
    except Exception as exc:
        return f"Error: work_update {action or 'unknown'} failed unexpectedly: {exc}"
    return f"Done. work_update {action}: {identity_module._public_result(result)}"


def set_active_task_timer(
    contact_id: str,
    *,
    timer_state: str,
    pause_reason: str | None = None,
    client: InterfaceClient | None = None,
) -> bool:
    """Best-effort external pause/resume using Glass-owned elapsed time."""
    task_id = cache_module.active_task_id(contact_id)
    if not task_id:
        return False
    cached = cache_module._task_cache(contact_id, task_id)
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
