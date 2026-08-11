"""Settling, closing, and writing a lifecycle down.
"""
from __future__ import annotations
from interface.work import cache as work_cache
from interface.work import updates as work_updates
from interface.long_tasks import constants
from interface.long_tasks import registry as registry_module
from interface.long_tasks import store as store_module
from interface.long_tasks import util as util_module
import os
import time
from copy import deepcopy
from typing import Any
from helpers.state import update_json


class LifecyclePersistenceMixin:
    def close_if_terminal(self) -> bool:
        """Tombstone and unregister a terminal lifecycle with no further turn."""
        with self._lock:
            if self._closed:
                return True
            if not self._terminal:
                return False
            removed_workers = self._discard_terminal_worker_updates_locked()
            if self._has_durable_delivery_locked():
                if removed_workers:
                    self._persist(active=True)
                return False
            self._close_locked()
        registry_module._unregister(self)
        return True


    def defer(
        self,
        note: str = "",
        *,
        pause_reason: str = "infrastructure",
    ) -> None:
        with self._lock:
            reason = (
                pause_reason
                if pause_reason
                in {"rate_limited", "offline", "infrastructure", "blocker"}
                else "infrastructure"
            )
            self._deferred = True
            self._defer_pause_reason = reason
            self._desired_timer_state = "paused"
            self._desired_pause_reason = reason
            self._timer_dirty = bool(self.task_id)
            self._next_timer_attempt_at = 0.0
            if note:
                self.latest_activity = util_module._compact(note, 200)
            self._persist(active=True)


    def finish(self, *, keep_alive: bool = False) -> None:
        """Stop manager heartbeats; recovery owns every outstanding intent."""
        with self._lock:
            self._manager_running = False
            if self._closed:
                return
            if keep_alive and not self._terminal:
                self.latest_activity = "Workers are processing the request"
            # Never infer successful completion merely because a manager turn
            # ended.  A final reply or explicit terminal action is the fence.
            terminal_without_delivery = (
                self._terminal
                and not self._has_durable_delivery_locked()
            )
            empty_ephemeral_lifecycle = (
                not self.task_id
                and not self.pending_reply
                and not self.pending_workers
                and not self._pending_create_spec
            )
            if terminal_without_delivery or empty_ephemeral_lifecycle:
                self._close_locked()
                should_unregister = True
            else:
                self._persist(active=True)
                should_unregister = False
        if should_unregister:
            registry_module._unregister(self)


    def _settle_task(self, *, close_terminal: bool = True) -> bool:
        try:
            return self._settle_task_inner()
        finally:
            # A remote terminal snapshot clears the durable settle guard.
            # Re-run cleanup after the IO lock is gone so queued roots can
            # advance even when this method is invoked outside the watcher.
            if close_terminal:
                self.close_if_terminal()


    def _settle_task_inner(self) -> bool:
        with self._io_lock:
            with self._lock:
                now = time.time()
                if (
                    not self.task_id
                    or not self.task_confirmed
                    or now < self._next_settle_attempt_at
                ):
                    return False
                task_id = self.task_id
            snapshot = work_cache.refresh_task_snapshot(self.contact_id, task_id)
            with self._lock:
                task_state = str(snapshot.get("state") or "")
                if task_state in constants._TERMINAL_STATES:
                    self._terminal = True
                    self._settle_requested = False
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return True
                if task_state == "blocked" or (
                    snapshot.get("timer_state") == "paused"
                    and snapshot.get("timer_pause_reason") == "blocker"
                ):
                    self._deferred = True
                    self._defer_pause_reason = "blocker"
                    self._desired_timer_state = "paused"
                    self._desired_pause_reason = "blocker"
                    self._timer_dirty = False
                    self.latest_activity = "Waiting for a blocker to be resolved"
                    self._settle_requested = True
                    self._persist(active=True)
                    return False

            terminal_spec = {
                "tool": "work_update",
                "action": "task/complete",
                "task_id": task_id,
                "data": {
                    "work_event_id": util_module._stable_id(
                        "complete-manager-task", task_id
                    ),
                    "body": "Request completed.",
                    "client_id": util_module._stable_id(
                        "complete-manager-task-client", task_id
                    ),
                },
            }
            terminal_result = work_updates.execute_work_update(
                terminal_spec, self.contact_id
            )
            accepted = util_module._successful(terminal_result)
            if not accepted:
                proof = work_cache.refresh_task_snapshot(self.contact_id, task_id)
                accepted = str(proof.get("state") or "") in constants._TERMINAL_STATES
            with self._lock:
                if not accepted:
                    self._schedule_settle_retry_locked(now)
                    return False
                self._terminal = True
                self._settle_requested = False
                self._settle_attempts = 0
                self._next_settle_attempt_at = 0.0
                self._desired_timer_state = "stopped"
                self._timer_dirty = False
                self._cancel_accuracy_schedule_locked()
                self._persist(active=True)
                return True


    def _close_locked(self) -> None:
        self._closed = True
        self._stop.set()
        entry = self._state_payload(active=False)
        now = time.time()

        def mutate(state: dict[str, Any]) -> None:
            contacts = state.setdefault("contacts", {})
            current = contacts.get(self.contact_id)
            if (
                isinstance(current, dict)
                and current.get("lease_owner") == self._lease_owner
            ):
                contacts[self.contact_id] = store_module._tombstone(entry, now)
            store_module._prune_state_locked(state, now)

        update_json(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)


    def _state_payload(self, *, active: bool) -> dict[str, Any]:
        return {
            "active": bool(active),
            "contact_id": self.contact_id,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "task_id": self.task_id,
            "task_confirmed": self.task_confirmed,
            "todo_id": self.todo_id,
            "title": util_module._compact(self.title, 120),
            "base_description": util_module._compact(self.base_description, 1_500),
            "latest_activity": util_module._compact(self.latest_activity, 200),
            "task_aliases": util_module._bounded_mapping(self.task_aliases),
            "todo_aliases": util_module._bounded_mapping(self.todo_aliases),
            "pending_workers": deepcopy(
                dict(list(self.pending_workers.items())[-constants.MAX_PENDING_WORKERS:])
            ),
            "worker_delivery_watermarks": deepcopy(
                dict(
                    list(self.worker_delivery_watermarks.items())[
                        -constants.MAX_PENDING_WORKERS:
                    ]
                )
            ),
            "pending_reply": deepcopy(self.pending_reply),
            "accuracy_schedule": deepcopy(self.accuracy_schedule),
            "pending_create_spec": deepcopy(self._pending_create_spec),
            "create_attempts": self._create_attempts,
            "next_create_attempt_at": self._next_create_attempt_at,
            "settle_attempts": self._settle_attempts,
            "next_settle_attempt_at": self._next_settle_attempt_at,
            "settle_requested": self._settle_requested,
            "deferred": self._deferred,
            "defer_pause_reason": self._defer_pause_reason,
            "terminal": self._terminal,
            "manager_running": self._manager_running,
            "desired_timer_state": self._desired_timer_state,
            "desired_pause_reason": self._desired_pause_reason,
            "timer_dirty": self._timer_dirty,
            "timer_attempts": self._timer_attempts,
            "next_timer_attempt_at": self._next_timer_attempt_at,
            "blocker_resolution_pending": self._blocker_resolution_pending,
            "last_durable_description": self._last_durable_description,
            "heartbeat_attempts": self._heartbeat_attempts,
            "next_heartbeat_attempt_at": self._next_heartbeat_attempt_at,
            "lease_owner": self._lease_owner,
            "lease_pid": os.getpid(),
            "lease_until": time.time() + constants.LEASE_SECONDS,
            "updated_at": time.time(),
        }


    def _persist(self, *, active: bool) -> bool:
        # Closing is a one-way state transition. A watcher action which began
        # just before _close_locked must never resurrect the tombstone.
        if active and self._closed:
            return False
        payload = self._state_payload(active=active)
        written = False

        def mutate(state: dict[str, Any]) -> None:
            nonlocal written
            if active and self._closed:
                return
            now = time.time()
            store_module._prune_state_locked(state, now)
            contacts = state.setdefault("contacts", {})
            current = contacts.get(self.contact_id)
            if isinstance(current, dict) and current.get("active"):
                owner = str(current.get("lease_owner") or "")
                lease_until = float(current.get("lease_until") or 0)
                if (
                    owner
                    and owner != self._lease_owner
                    and lease_until > now
                    and util_module._pid_alive(current.get("lease_pid"))
                ):
                    return
                # A worker process can publish a newer runtime fact directly
                # into the journal. Do not overwrite it with a stale copy.
                current_workers = current.get("pending_workers")
                if isinstance(current_workers, dict):
                    for worker_id, external in current_workers.items():
                        local = payload["pending_workers"].get(worker_id)
                        external_updated = float(
                            external.get("fact_updated_at") or 0
                        ) if isinstance(external, dict) else 0.0
                        watermark = float(
                            payload["worker_delivery_watermarks"].get(
                                worker_id
                            )
                            or 0
                        )
                        if (
                            isinstance(external, dict)
                            and isinstance(local, dict)
                            and external_updated
                            > float(local.get("fact_updated_at") or 0)
                        ):
                            payload["pending_workers"][worker_id] = deepcopy(
                                external
                            )
                        elif (
                            isinstance(external, dict)
                            and not isinstance(local, dict)
                            and external_updated > watermark
                        ):
                            restored = deepcopy(external)
                            restored["phase"] = "published"
                            payload["pending_workers"][worker_id] = restored
            contacts[self.contact_id] = deepcopy(payload)
            written = True

        update_json(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)
        return written
