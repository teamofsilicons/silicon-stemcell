"""What every long task is: identity, its contact, and its open state.
"""
from __future__ import annotations
from interface.work import cache as work_cache
from interface.work import updates as work_updates

from interface.long_tasks import constants
from interface.long_tasks import store as store_module
from interface.long_tasks import util as util_module
import threading
import time
import uuid
from copy import deepcopy
from typing import Any, Callable


class LifecycleBase:
    def __init__(
        self,
        contact_id: str,
        run_id: str,
        context: str,
        *,
        activity_heartbeat: Callable[[str], None] | None = None,
        activity_heartbeat_seconds: float = constants.ACTIVITY_HEARTBEAT_SECONDS,
        durable_heartbeat_seconds: float = constants.DURABLE_HEARTBEAT_SECONDS,
        saved: dict[str, Any] | None = None,
        auto_start: bool = True,
        lease_owner: str = "",
        recovery: bool = False,
        reply_sender: Callable[..., str] | None = None,
        has_active_workers: Callable[[str], bool] | None = None,
        worker_status_resolver: Callable[[str, str], str] | None = None,
    ):
        self.contact_id = str(contact_id)
        self.run_id = str(run_id or util_module._stable_id("run", contact_id, context))
        self.started_at = time.time()
        self.activity_heartbeat_seconds = max(
            0.1, float(activity_heartbeat_seconds)
        )
        self.durable_heartbeat_seconds = max(
            self.activity_heartbeat_seconds,
            float(durable_heartbeat_seconds),
        )
        self.activity_heartbeat = activity_heartbeat
        self.reply_sender = reply_sender
        self.has_active_workers = has_active_workers
        self.worker_status_resolver = worker_status_resolver
        self.title = util_module._title_from_context(context)
        self.task_id = ""
        self.task_confirmed = False
        self.todo_id = ""
        self.base_description = (
            "Work is underway. This card stays current until the request is complete."
        )
        self.latest_activity = "Working through the request"
        self.task_aliases: dict[str, str] = {}
        self.todo_aliases: dict[str, str] = {}
        self.pending_workers: dict[str, dict[str, Any]] = {}
        self.worker_delivery_watermarks: dict[str, float] = {}
        self.pending_reply: dict[str, Any] = {}
        self.accuracy_schedule: dict[str, Any] = {}
        self._pending_create_spec: dict[str, Any] = {}
        self._desired_timer_state = "running"
        self._desired_pause_reason = ""
        self._timer_dirty = False
        self._timer_attempts = 0
        self._next_timer_attempt_at = 0.0
        self._blocker_resolution_pending = False
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._reply_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._manager_running = not recovery
        self._final_reply_sent = False
        self._deferred = False
        self._defer_pause_reason = "infrastructure"
        self._terminal = False
        self._model_create_started_at = 0.0
        self._runtime_create_inflight = False
        self._create_attempts = 0
        self._next_create_attempt_at = 0.0
        self._settle_attempts = 0
        self._next_settle_attempt_at = 0.0
        self._settle_requested = False
        self._heartbeat_attempts = 0
        self._next_heartbeat_attempt_at = 0.0
        self._last_activity_heartbeat_at = self.started_at
        self._last_durable_heartbeat_at = self.started_at
        self._last_durable_description = ""
        self._lease_owner = str(
            lease_owner or f"{constants._PROCESS_TOKEN}:{uuid.uuid4().hex}"
        )
        self._recovery_mode = bool(recovery)

        saved = saved if isinstance(saved, dict) else {}
        if saved.get("active"):
            self.run_id = str(saved.get("run_id") or self.run_id)
            self.started_at = float(saved.get("started_at") or self.started_at)
            self.task_id = str(saved.get("task_id") or "")
            self.task_confirmed = bool(saved.get("task_confirmed"))
            self.todo_id = str(saved.get("todo_id") or self.todo_id)
            self.base_description = util_module._compact(
                saved.get("base_description") or self.base_description,
                1_500,
            )
            self.title = util_module._compact(saved.get("title") or self.title, 120)
            self.latest_activity = util_module._compact(
                saved.get("latest_activity") or self.latest_activity,
                200,
            )
            self.task_aliases = util_module._bounded_mapping(saved.get("task_aliases"))
            self.todo_aliases = util_module._bounded_mapping(saved.get("todo_aliases"))
            workers = saved.get("pending_workers")
            if isinstance(workers, dict):
                for worker_id, intent in list(workers.items())[
                    -constants.MAX_PENDING_WORKERS:
                ]:
                    if isinstance(intent, dict):
                        self.pending_workers[str(worker_id)] = deepcopy(intent)
            watermarks = saved.get("worker_delivery_watermarks")
            if isinstance(watermarks, dict):
                self.worker_delivery_watermarks = {
                    str(worker_id): float(value or 0)
                    for worker_id, value in list(watermarks.items())[
                        -constants.MAX_PENDING_WORKERS:
                    ]
                }
            pending_reply = saved.get("pending_reply")
            if isinstance(pending_reply, dict):
                self.pending_reply = deepcopy(pending_reply)
            accuracy_schedule = saved.get("accuracy_schedule")
            if isinstance(accuracy_schedule, dict):
                self.accuracy_schedule = deepcopy(accuracy_schedule)
            pending_create = saved.get("pending_create_spec")
            if isinstance(pending_create, dict):
                self._pending_create_spec = deepcopy(pending_create)
            self._create_attempts = int(saved.get("create_attempts") or 0)
            self._next_create_attempt_at = float(
                saved.get("next_create_attempt_at") or 0
            )
            self._settle_attempts = int(saved.get("settle_attempts") or 0)
            self._next_settle_attempt_at = float(
                saved.get("next_settle_attempt_at") or 0
            )
            self._settle_requested = bool(saved.get("settle_requested"))
            self._last_durable_description = str(
                saved.get("last_durable_description") or ""
            )
            self._heartbeat_attempts = int(
                saved.get("heartbeat_attempts") or 0
            )
            self._next_heartbeat_attempt_at = float(
                saved.get("next_heartbeat_attempt_at") or 0
            )
            self._defer_pause_reason = str(
                saved.get("defer_pause_reason") or "infrastructure"
            )
            self._deferred = bool(saved.get("deferred"))
            self._terminal = bool(saved.get("terminal"))
            self._desired_timer_state = str(
                saved.get("desired_timer_state") or "running"
            )
            self._desired_pause_reason = str(
                saved.get("desired_pause_reason") or ""
            )
            self._timer_dirty = bool(saved.get("timer_dirty"))
            self._timer_attempts = int(saved.get("timer_attempts") or 0)
            self._next_timer_attempt_at = float(
                saved.get("next_timer_attempt_at") or 0
            )
            self._blocker_resolution_pending = bool(
                saved.get("blocker_resolution_pending")
            )
            if (
                recovery
                and bool(saved.get("manager_running"))
                and not self.pending_reply
                and not self._terminal
                and not self._deferred
            ):
                # The manager process cannot resume a lost provider turn.  Keep
                # the card honest and recoverable instead of claiming progress.
                self._deferred = True
                self._defer_pause_reason = "infrastructure"
                self._desired_timer_state = "paused"
                self._desired_pause_reason = "infrastructure"
                self._timer_dirty = True

        claimed = store_module._claim_contact(
            self.contact_id,
            self._lease_owner,
            expected_run_id=self.run_id,
            allow_create=not bool(saved.get("active")),
        )
        if claimed is None:
            self._closed = True
            self._stop.set()
        else:
            self._persist(active=True)
        if auto_start and not self._closed:
            self.start()


    @property
    def is_open(self) -> bool:
        with self._lock:
            return not self._closed


    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(
                target=self._watch,
                name=f"long-task-{self.contact_id[:24]}",
                daemon=True,
            )
            self._thread.start()


    def attach(
        self,
        run_id: str,
        context: str,
        activity_heartbeat: Callable[[str], None] | None,
    ) -> None:
        """Attach a continuation without overwriting unresolved durable intent."""
        with self._lock:
            if self._closed:
                return
            self._manager_running = True
            if self._defer_pause_reason != "blocker":
                self._deferred = False
                self._defer_pause_reason = "infrastructure"
                self._desired_timer_state = "running"
                self._desired_pause_reason = ""
                self._timer_dirty = bool(self.task_id)
                self._next_timer_attempt_at = 0.0
            if activity_heartbeat is not None:
                self.activity_heartbeat = activity_heartbeat
            if not self.title or self.title == "Working on your request":
                self.title = util_module._title_from_context(context)
            self._persist(active=True)


    def observe(self, state: str) -> None:
        note = constants._SAFE_ACTIVITY_NOTES.get(
            str(state), constants._SAFE_ACTIVITY_NOTES["working"]
        )
        with self._lock:
            self.latest_activity = note


    def resolve_task_id(self, requested: str = "") -> str:
        with self._lock:
            requested = str(requested or "")
            return self.task_aliases.get(requested, requested or self.task_id)


    def continuing_round(self) -> str:
        with self._lock:
            if self.pending_reply or self._deferred or self._terminal:
                return self.task_id
        self.observe("continuing")
        return self.ensure("continuing")


    def request_running(self) -> None:
        with self._lock:
            if self._defer_pause_reason == "blocker":
                self._timer_dirty = True
            else:
                self._deferred = False
                self._desired_timer_state = "running"
                self._desired_pause_reason = ""
                self._timer_dirty = bool(self.task_id)
            self._next_timer_attempt_at = 0.0
            self._persist(active=True)


    def ensure(self, reason: str = "working") -> str:
        """Replay only an exact manager-authored task/create intent."""
        with self._io_lock:
            with self._lock:
                if self._closed or self._terminal or not self._renew_lease_locked():
                    return self.task_id if self.task_confirmed else ""
                if reason:
                    self.observe(reason)
                if self.task_id and self.task_confirmed:
                    return self.task_id
                now = time.time()
                if now < self._next_create_attempt_at:
                    return ""
                if self._model_create_started_at or self._runtime_create_inflight:
                    return ""
                spec = deepcopy(self._pending_create_spec)
                if not spec:
                    return ""
                intended_task_id = str(
                    (spec.get("data") or {}).get("task_id") or self.task_id
                )
                if not intended_task_id:
                    return ""
                self.task_id = intended_task_id
                self._runtime_create_inflight = True
                self._persist(active=True)

            result = work_updates.execute_work_update(spec, self.contact_id)
            snapshot = (
                {}
                if util_module._successful(result)
                else work_cache.refresh_task_snapshot(self.contact_id, intended_task_id)
            )
            with self._lock:
                self._runtime_create_inflight = False
                accepted = util_module._successful(result) or (
                    str(snapshot.get("task_id") or "") == intended_task_id
                )
                if accepted:
                    self.task_confirmed = True
                    self._pending_create_spec = {}
                    self._create_attempts = 0
                    self._next_create_attempt_at = 0.0
                    self._last_durable_heartbeat_at = time.time()
                    self._last_durable_description = str(
                        snapshot.get("description") or self.base_description
                    )
                    self._timer_dirty = True
                    self._schedule_accuracy_from_data_locked(
                        intended_task_id,
                        spec.get("data"),
                    )
                    self._persist(active=True)
                    return self.task_id
                self._create_attempts += 1
                self._next_create_attempt_at = util_module._retry_at(self._create_attempts)
                self._persist(active=True)
                return ""


    def _activity_description(self, activity: str) -> str:
        description = self.base_description.strip()
        if description:
            description += "\n\n"
        return description + f"Latest activity: {util_module._compact(activity, 200)}."


    def _has_active_workers_now(self) -> bool:
        callback = self.has_active_workers
        if callback is None:
            return bool(self.pending_workers)
        try:
            return bool(callback(self.contact_id))
        except Exception:
            return True
