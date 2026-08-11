"""The watch loop, the reconcile cadence, and the heartbeat that holds the lease.
"""
from __future__ import annotations
from interface.work import cache as work_cache
from interface.work import updates as work_updates
from interface.long_tasks import constants
from interface.long_tasks import store as store_module
from interface.long_tasks import util as util_module
import time
from typing import Any


class CadenceMixin:
    def _watch(self) -> None:
        self.replay_pending_once(recovery=self._recovery_mode)
        self._recovery_mode = False
        while not self._stop.wait(1.0):
            self._reconcile_worker_intents()
            now = time.time()
            with self._lock:
                if self._closed or not self._renew_lease_locked():
                    return
                manager_running = self._manager_running
                activity_due = (
                    manager_running
                    and now - self._last_activity_heartbeat_at
                    >= self.activity_heartbeat_seconds
                )
                create_due = (
                    bool(self._pending_create_spec)
                    and not self.task_confirmed
                    and now >= self._next_create_attempt_at
                    and not self._model_create_started_at
                )
                durable_due = (
                    bool(self.task_id and self.task_confirmed)
                    and now - self._last_durable_heartbeat_at
                    >= self.durable_heartbeat_seconds
                    and now >= self._next_heartbeat_attempt_at
                )
                timer_due = (
                    self._timer_dirty
                    and self.task_confirmed
                    and now >= self._next_timer_attempt_at
                )
                settle_due = (
                    self._settle_requested
                    and self.task_confirmed
                    and not self.pending_reply
                    and now >= self._next_settle_attempt_at
                )
                worker_due = any(
                    isinstance(item, dict)
                    and item.get("phase") in {"launched", "published"}
                    and now >= float(item.get("next_attempt_at") or 0)
                    for item in self.pending_workers.values()
                )
                reply_due = bool(self.pending_reply) and now >= float(
                    self.pending_reply.get("next_attempt_at") or 0
                )
                activity = self.latest_activity
                if activity_due:
                    self._last_activity_heartbeat_at = now
            if activity_due and self.activity_heartbeat is not None:
                try:
                    self.activity_heartbeat(activity)
                except Exception:
                    pass
            if create_due:
                self.ensure("")
            if worker_due:
                self._deliver_pending_workers()
            if timer_due:
                self._reconcile_timer()
            if settle_due:
                self._settle_task()
            if reply_due:
                self._flush_final_reply()
            elif durable_due:
                self._heartbeat(activity)
            self._prepare_accuracy_review_if_due()
            if self.close_if_terminal():
                return


    def _reconcile_timer(self, *, force: bool = False) -> bool:
        with self._io_lock:
            with self._lock:
                if (
                    not self.task_id
                    or not self.task_confirmed
                    or self._terminal
                    or (
                        not force
                        and time.time() < self._next_timer_attempt_at
                    )
                ):
                    return False
                task_id = self.task_id
                desired_state = self._desired_timer_state
                desired_reason = self._desired_pause_reason
                blocker_resolution_pending = self._blocker_resolution_pending
            snapshot = work_cache.refresh_task_snapshot(self.contact_id, task_id)
            with self._lock:
                if task_id != self.task_id:
                    return False
                remote_state = str(snapshot.get("state") or "")
                if remote_state in constants._TERMINAL_STATES:
                    self._terminal = True
                    self._timer_dirty = False
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return True
                remote_timer = str(snapshot.get("timer_state") or "")
                remote_reason = str(snapshot.get("timer_pause_reason") or "")
                if blocker_resolution_pending:
                    if remote_timer == "paused" and remote_reason == "blocker":
                        self._deferred = True
                        self._defer_pause_reason = "blocker"
                        self._desired_timer_state = "paused"
                        self._desired_pause_reason = "blocker"
                    else:
                        self._deferred = False
                        self._defer_pause_reason = "infrastructure"
                        self._desired_timer_state = "running"
                        self._desired_pause_reason = ""
                    self._blocker_resolution_pending = False
                    desired_state = self._desired_timer_state
                    desired_reason = self._desired_pause_reason
                if (
                    desired_state == "running"
                    and remote_timer == "paused"
                    and remote_reason == "blocker"
                ):
                    self._deferred = True
                    self._defer_pause_reason = "blocker"
                    self._desired_timer_state = "paused"
                    self._desired_pause_reason = "blocker"
                    self._timer_dirty = False
                    self._persist(active=True)
                    return True
                matches = remote_timer == desired_state and (
                    desired_state != "paused" or remote_reason == desired_reason
                )
                if matches:
                    self._timer_dirty = False
                    self._timer_attempts = 0
                    self._next_timer_attempt_at = 0.0
                    self._persist(active=True)
                    return True
                if not snapshot:
                    self._timer_attempts += 1
                    self._next_timer_attempt_at = util_module._retry_at(
                        self._timer_attempts
                    )
                    self._persist(active=True)
                    return False
                data: dict[str, Any] = {"timer_state": desired_state}
                data["timer_pause_reason"] = (
                    desired_reason if desired_state == "paused" else None
                )
            result = work_updates.execute_work_update(
                {
                    "tool": "work_update",
                    "action": "task/update",
                    "task_id": task_id,
                    "data": data,
                },
                self.contact_id,
            )
            with self._lock:
                if util_module._successful(result):
                    self._timer_dirty = False
                    self._timer_attempts = 0
                    self._next_timer_attempt_at = 0.0
                    self._persist(active=True)
                    return True
                # Keep desired state durable. A later refresh proves a lost
                # response without issuing conflicting timer transitions.
                self._timer_attempts += 1
                self._next_timer_attempt_at = util_module._retry_at(self._timer_attempts)
                self._persist(active=True)
                return False


    def _heartbeat(self, activity: str) -> bool:
        with self._io_lock:
            with self._lock:
                if not self.task_id or not self.task_confirmed or self._terminal:
                    return False
                task_id = self.task_id
                description = self._activity_description(activity)
                if description == self._last_durable_description:
                    self._last_durable_heartbeat_at = time.time()
                    return True
            snapshot = work_cache.refresh_task_snapshot(self.contact_id, task_id)
            with self._lock:
                if task_id != self.task_id or self._terminal:
                    return False
                if snapshot.get("state") in constants._TERMINAL_STATES:
                    self._terminal = True
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return True
                remote_description = str(snapshot.get("description") or "")
                if (
                    remote_description
                    and remote_description != self._last_durable_description
                    and remote_description != description
                ):
                    marker = "\n\nLatest activity:"
                    self.base_description = (
                        remote_description.rsplit(marker, 1)[0]
                        if marker in remote_description
                        else remote_description
                    )
                    description = self._activity_description(activity)
                if remote_description == description:
                    self._record_heartbeat_success_locked(description)
                    return True
                spec = {
                    "tool": "work_update",
                    "action": "task/update",
                    "task_id": task_id,
                    "data": {"description": description},
                }
            result = work_updates.execute_work_update(spec, self.contact_id)
            with self._lock:
                if util_module._successful(result):
                    self._record_heartbeat_success_locked(description)
                    return True
                self._heartbeat_attempts += 1
                self._next_heartbeat_attempt_at = util_module._retry_at(
                    self._heartbeat_attempts
                )
                self._persist(active=True)
                return False


    def _record_heartbeat_success_locked(self, description: str) -> None:
        self._last_durable_description = description
        self._last_durable_heartbeat_at = time.time()
        self._heartbeat_attempts = 0
        self._next_heartbeat_attempt_at = 0.0
        self._persist(active=True)


    def _schedule_settle_retry_locked(self, _now: float) -> None:
        self._settle_requested = True
        self._settle_attempts += 1
        self._next_settle_attempt_at = util_module._retry_at(self._settle_attempts)
        self._persist(active=True)


    def _renew_lease_locked(self) -> bool:
        claimed = store_module._claim_contact(
            self.contact_id,
            self._lease_owner,
            expected_run_id=self.run_id,
            allow_create=False,
        )
        return claimed is not None
