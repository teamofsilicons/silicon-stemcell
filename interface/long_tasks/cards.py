"""Turning a manager's work_update into changes on the durable card.
"""
from __future__ import annotations
from interface.work import cache as work_cache

from interface.long_tasks import constants
from interface.long_tasks import util as util_module
import time
from copy import deepcopy
from typing import Any


class CardMixin:
    def prepare_work_update(
        self,
        tool_spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        with self._io_lock:
            return self._prepare_work_update_locked(tool_spec)


    def _prepare_work_update_locked(
        self,
        tool_spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        original = deepcopy(tool_spec)
        action = str(original.get("action") or original.get("type") or "").lower()
        if action not in {"task/create"} | constants._TERMINAL_ACTIONS and not self.task_id:
            return [original]

        with self._lock:
            requested_task_id = str(
                original.get("task_id")
                or (original.get("data") or {}).get("task_id")
                or ""
            )
            mapped_task_id = self.task_aliases.get(requested_task_id, "")
            if mapped_task_id:
                original["task_id"] = mapped_task_id
                if isinstance(original.get("data"), dict):
                    original["data"].pop("task_id", None)
            elif not requested_task_id and self.task_id:
                original["task_id"] = self.task_id

            requested_todo_id = str(
                original.get("todo_id")
                or (original.get("data") or {}).get("todo_id")
                or ""
            )
            if requested_todo_id:
                original["todo_id"] = self.todo_aliases.get(
                    requested_todo_id, requested_todo_id
                )
                if isinstance(original.get("data"), dict):
                    original["data"].pop("todo_id", None)

            if action != "task/create":
                return [original]

            data = deepcopy(original.get("data") or {})
            todos = data.get("todos")
            if not self.task_id:
                self.task_id = str(
                    data.get("task_id")
                    or util_module._stable_id("task-model", self.contact_id, self.run_id)
                )
                data["task_id"] = self.task_id
                data.setdefault(
                    "client_id",
                    util_module._stable_id("create-model-task", self.contact_id, self.run_id),
                )
                if isinstance(todos, list) and todos:
                    first = todos[0]
                    if isinstance(first, dict):
                        self.todo_id = str(
                            first.get("todo_id")
                            or util_module._stable_id("todo-model", self.task_id, 0)
                        )
                        first["todo_id"] = self.todo_id
                prepared = {
                    "tool": "work_update",
                    "action": "task/create",
                    "data": data,
                }
                self._pending_create_spec = deepcopy(prepared)
                self._model_create_started_at = time.time()
                self._persist(active=True)
                return [prepared]

            if requested_task_id:
                self.task_aliases[requested_task_id] = self.task_id
                self.task_aliases = util_module._bounded_mapping(self.task_aliases)
            todos = data.pop("todos", [])
            if not self.task_confirmed:
                data["task_id"] = self.task_id
                data["client_id"] = util_module._stable_id(
                    "create-model-task", self.contact_id, self.run_id
                )
                if isinstance(todos, list) and todos and isinstance(
                    todos[0], dict
                ):
                    first_requested_id = str(todos[0].get("todo_id") or "")
                    if not self.todo_id:
                        self.todo_id = (
                            first_requested_id
                            or util_module._stable_id("todo-model", self.task_id, 0)
                        )
                    if first_requested_id and self.todo_id:
                        self.todo_aliases[first_requested_id] = self.todo_id
                        self.todo_aliases = util_module._bounded_mapping(self.todo_aliases)
                    todos[0]["todo_id"] = self.todo_id
                if isinstance(todos, list):
                    data["todos"] = todos
                prepared = {
                    "tool": "work_update",
                    "action": "task/create",
                    "data": data,
                }
                self._pending_create_spec = deepcopy(prepared)
                self._model_create_started_at = time.time()
                self._persist(active=True)
                return [prepared]

            for key in ("task_id", "client_id", "room_id", "schema_version"):
                data.pop(key, None)
            rewritten = [
                {
                    "tool": "work_update",
                    "action": "task/update",
                    "task_id": self.task_id,
                    "data": data,
                }
            ]
            if isinstance(todos, list) and todos:
                first = deepcopy(todos[0]) if isinstance(todos[0], dict) else {}
                first_id = str(first.pop("todo_id", "") or "")
                if self.todo_id:
                    first.pop("client_id", None)
                    if first_id:
                        self.todo_aliases[first_id] = self.todo_id
                    rewritten.append(
                        {
                            "tool": "work_update",
                            "action": "todo/update",
                            "task_id": self.task_id,
                            "todo_id": self.todo_id,
                            "data": first,
                        }
                    )
                    remaining_todos = todos[1:]
                    start_index = 1
                else:
                    remaining_todos = todos
                    start_index = 0
                for index, todo in enumerate(
                    remaining_todos,
                    start_index,
                ):
                    if not isinstance(todo, dict):
                        continue
                    item = deepcopy(todo)
                    item.setdefault(
                        "todo_id",
                        util_module._stable_id(
                            "todo-manager-adopted",
                            self.task_id,
                            index,
                            item.get("title"),
                        ),
                    )
                    if not self.todo_id:
                        self.todo_id = str(item["todo_id"])
                    item.setdefault(
                        "client_id",
                        util_module._stable_id("add-manager-todo", self.task_id, item["todo_id"]),
                    )
                    rewritten.append(
                        {
                            "tool": "work_update",
                            "action": "todo/add",
                            "task_id": self.task_id,
                            "data": item,
                        }
                    )
            self.todo_aliases = util_module._bounded_mapping(self.todo_aliases)
            self._persist(active=True)
            return rewritten


    def record_work_update(
        self,
        original: dict[str, Any],
        prepared: list[dict[str, Any]],
        results: list[Any],
    ) -> None:
        action = str(original.get("action") or original.get("type") or "").lower()
        successful = bool(results) and all(util_module._successful(item) for item in results)
        requested_task_id = str(
            original.get("task_id")
            or (original.get("data") or {}).get("task_id")
            or ""
        )
        with self._lock:
            targets_current = (
                not requested_task_id
                or self.resolve_task_id(requested_task_id) == self.task_id
                or action == "task/create"
            )
            if action == "task/create":
                self._model_create_started_at = 0.0
                if successful:
                    accepted_id = str(
                        self.task_id
                        or work_cache.active_task_id(self.contact_id)
                        or requested_task_id
                    )
                    if accepted_id:
                        self.task_id = accepted_id
                        self.task_confirmed = True
                        self._pending_create_spec = {}
                        self._create_attempts = 0
                        self._next_create_attempt_at = 0.0
                else:
                    self._create_attempts += 1
                    self._next_create_attempt_at = util_module._retry_at(
                        self._create_attempts
                    )
                data = original.get("data")
                if successful and isinstance(data, dict):
                    requested = str(data.get("task_id") or "")
                    if requested and self.task_id:
                        self.task_aliases[requested] = self.task_id
                    if data.get("title"):
                        self.title = util_module._compact(data["title"], 120)
                    if data.get("description"):
                        self.base_description = util_module._compact(
                            data["description"], 1_500
                        )
                    self._schedule_accuracy_from_data_locked(
                        self.task_id,
                        data,
                    )
            elif action == "task/update" and successful and targets_current:
                data = original.get("data")
                if isinstance(data, dict):
                    if data.get("description"):
                        self.base_description = util_module._compact(
                            data["description"], 1_500
                        )
                    timer_state = str(data.get("timer_state") or "")
                    if timer_state in {"running", "paused"}:
                        self._desired_timer_state = timer_state
                        self._desired_pause_reason = str(
                            data.get("timer_pause_reason") or ""
                        )
                        self._timer_dirty = True
                    self._schedule_accuracy_from_data_locked(
                        self.task_id,
                        data,
                    )
            elif action == "blocker/create" and successful and targets_current:
                self._deferred = True
                self._defer_pause_reason = "blocker"
                self._desired_timer_state = "paused"
                self._desired_pause_reason = "blocker"
                self._timer_dirty = True
                self.latest_activity = "Waiting for a blocker to be resolved"
            elif action == "blocker/resolve" and successful and targets_current:
                self._blocker_resolution_pending = True
                self._timer_dirty = True
                self._next_timer_attempt_at = 0.0
            elif action in constants._TERMINAL_ACTIONS and successful and targets_current:
                self._terminal = True
                self._settle_requested = False
                self._desired_timer_state = "stopped"
                self._timer_dirty = False
                self._cancel_accuracy_schedule_locked()
            self.task_aliases = util_module._bounded_mapping(self.task_aliases)
            self._persist(active=True)


    def _has_durable_delivery_locked(self) -> bool:
        return bool(
            self.pending_reply
            or self.pending_workers
            or self._pending_create_spec
            or self._runtime_create_inflight
            or self._settle_requested
        )


    def _discard_terminal_worker_updates_locked(self) -> bool:
        """Cancel card mutations made obsolete by an accepted terminal task."""
        removed = False
        now = time.time()
        for worker_id, intent in list(self.pending_workers.items()):
            if (
                not isinstance(intent, dict)
                or intent.get("phase") not in {"launched", "published"}
            ):
                continue
            self.pending_workers.pop(worker_id, None)
            self.worker_delivery_watermarks[str(worker_id)] = max(
                now,
                float(intent.get("fact_updated_at") or 0),
            )
            removed = True
        return removed
