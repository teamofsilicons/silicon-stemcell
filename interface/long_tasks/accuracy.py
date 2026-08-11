"""Deciding when a task's own work should be reviewed, and running that review.
"""
from __future__ import annotations
from interface.work import cache as work_cache

from interface.long_tasks import constants
from interface.long_tasks import util as util_module
import json
import math
import os
import time
from copy import deepcopy
from typing import Any


class AccuracyReviewMixin:
    def _set_accuracy_goal_locked(
        self,
        task_id: str,
        goal_seconds: float,
        *,
        now: float | None = None,
    ) -> bool:
        task_id = str(task_id or "")
        goal_seconds = util_module._non_negative_number(goal_seconds)
        if not task_id or not goal_seconds:
            return False
        existing = self.accuracy_schedule
        if (
            isinstance(existing, dict)
            and existing.get("task_id") == task_id
            and not util_module._goal_materially_changed(
                float(existing.get("goal_seconds") or 0),
                goal_seconds,
            )
        ):
            return False
        now = float(now or time.time())
        generation = util_module._stable_id(
            "accuracy-schedule",
            self.contact_id,
            task_id,
            goal_seconds,
            time.time_ns(),
        )
        self.accuracy_schedule = {
            "task_id": task_id,
            "goal_seconds": goal_seconds,
            "interval_seconds": goal_seconds / constants.ACCURACY_REVIEW_SEGMENTS,
            "anchor_at": now,
            "next_checkpoint": 1,
            "generation": generation,
            "pending_review": {},
            "refresh_attempts": 0,
            "next_refresh_attempt_at": 0.0,
            "updated_at": now,
        }
        return True


    def _schedule_accuracy_from_data_locked(
        self,
        task_id: str,
        data: Any,
    ) -> bool:
        estimate_present, goal_seconds = util_module._estimate_goal_from_data(data)
        if not goal_seconds:
            if (
                estimate_present
                and isinstance(self.accuracy_schedule, dict)
                and self.accuracy_schedule.get("task_id") == str(task_id)
            ):
                self._cancel_accuracy_schedule_locked()
                return True
            return False
        return self._set_accuracy_goal_locked(task_id, goal_seconds)


    def _cancel_accuracy_schedule_locked(self) -> None:
        self.accuracy_schedule = {}


    def _accuracy_review_context(
        self,
        *,
        schedule: dict[str, Any],
        snapshot: dict[str, Any],
        checkpoint_from: int,
        checkpoint_through: int,
    ) -> str:
        task_id = str(schedule.get("task_id") or self.task_id)
        goal_seconds = float(schedule.get("goal_seconds") or 0)
        interval_seconds = float(schedule.get("interval_seconds") or 0)
        snapshot_json = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        context = (
            "Internal task accuracy review. This is not a user message and "
            "must not produce a normal reply.\n"
            f"task_id: {task_id}\n"
            f"accepted_goal_seconds: {goal_seconds:g}\n"
            f"review_interval_seconds: {interval_seconds:g}\n"
            f"checkpoint_from: {checkpoint_from}\n"
            f"checkpoint_through: {checkpoint_through}\n"
            "Inspect the accepted task, Todos, estimate, timer, blockers, "
            "workers, and current execution facts. Publish work_update "
            "mutations only where the durable card is materially inaccurate "
            "or stale. Otherwise use do_nothing. Do not call reply or contact "
            "another manager solely for this review.\n"
            f"accepted_task_snapshot: {snapshot_json}"
        )
        if len(context) > constants.MAX_ACCURACY_REVIEW_CONTEXT_CHARS:
            context = (
                context[: constants.MAX_ACCURACY_REVIEW_CONTEXT_CHARS - 1] + "…"
            )
        return context


    def _prepare_accuracy_review_if_due(self) -> bool:
        """Materialize at most one coalesced internal review root."""
        with self._io_lock:
            with self._lock:
                schedule = deepcopy(self.accuracy_schedule)
                if (
                    self._closed
                    or self._terminal
                    or not schedule
                    or schedule.get("pending_review")
                ):
                    return False
                now = time.time()
                if now < float(
                    schedule.get("next_refresh_attempt_at") or 0
                ):
                    return False
                interval = util_module._non_negative_number(
                    schedule.get("interval_seconds")
                )
                anchor_at = float(schedule.get("anchor_at") or now)
                next_checkpoint = max(
                    1, int(schedule.get("next_checkpoint") or 1)
                )
                if (
                    not interval
                    or now < anchor_at + next_checkpoint * interval
                ):
                    return False
                task_id = str(schedule.get("task_id") or "")
                generation = str(schedule.get("generation") or "")

            snapshot = work_cache.refresh_task_snapshot(self.contact_id, task_id)
            if str(snapshot.get("state") or "") in constants._TERMINAL_STATES:
                with self._lock:
                    current = self.accuracy_schedule
                    if (
                        self._closed
                        or not current
                        or current.get("generation") != generation
                        or current.get("pending_review")
                    ):
                        return False
                    self._terminal = True
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                self.close_if_terminal()
                return False
            with self._lock:
                current = self.accuracy_schedule
                if (
                    not current
                    or current.get("generation") != generation
                    or current.get("pending_review")
                    or self._terminal
                ):
                    return False
                if not snapshot:
                    attempts = int(current.get("refresh_attempts") or 0) + 1
                    current["refresh_attempts"] = attempts
                    current["next_refresh_attempt_at"] = util_module._retry_at(attempts)
                    current["updated_at"] = time.time()
                    self._persist(active=True)
                    return False
                estimate_present, accepted_goal = util_module._estimate_goal_from_data(
                    snapshot
                )
                if estimate_present and not accepted_goal:
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return False
                if accepted_goal and util_module._goal_materially_changed(
                    float(current.get("goal_seconds") or 0),
                    accepted_goal,
                ):
                    self._set_accuracy_goal_locked(
                        task_id,
                        accepted_goal,
                        now=time.time(),
                    )
                    self._persist(active=True)
                    return False

                now = time.time()
                interval = util_module._non_negative_number(
                    current.get("interval_seconds")
                )
                anchor_at = float(current.get("anchor_at") or now)
                checkpoint_from = max(
                    1, int(current.get("next_checkpoint") or 1)
                )
                if not interval:
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return False
                checkpoint_through = int(
                    math.floor(
                        max(0.0, now - anchor_at) / interval + 1e-9
                    )
                )
                if checkpoint_through < checkpoint_from:
                    return False
                review_id = util_module._stable_id(
                    "accuracy-review",
                    current.get("generation"),
                    checkpoint_from,
                    checkpoint_through,
                )
                context = self._accuracy_review_context(
                    schedule=current,
                    snapshot=snapshot,
                    checkpoint_from=checkpoint_from,
                    checkpoint_through=checkpoint_through,
                )
                current["pending_review"] = {
                    "review_id": review_id,
                    "context": context,
                    "checkpoint_from": checkpoint_from,
                    "checkpoint_through": checkpoint_through,
                    "phase": "pending",
                    "claim_owner": "",
                    "claim_pid": 0,
                    "claim_until": 0.0,
                    "created_at": now,
                }
                current["refresh_attempts"] = 0
                current["next_refresh_attempt_at"] = 0.0
                current["updated_at"] = now
                self._persist(active=True)
                return True


    def claim_accuracy_review(
        self,
        *,
        owner: str,
        now: float,
    ) -> tuple[str, str] | None:
        with self._lock:
            schedule = self.accuracy_schedule
            pending = (
                schedule.get("pending_review")
                if isinstance(schedule, dict)
                else None
            )
            if (
                self._closed
                or self._terminal
                or not isinstance(pending, dict)
                or not pending.get("review_id")
                or pending.get("phase") == "dispatched"
            ):
                return None
            claim_owner = str(pending.get("claim_owner") or "")
            if (
                claim_owner
                and float(pending.get("claim_until") or 0) > now
                and util_module._pid_alive(pending.get("claim_pid"))
            ):
                return None
            pending["phase"] = "claimed"
            pending["claim_owner"] = str(owner)
            pending["claim_pid"] = os.getpid()
            pending["claim_until"] = (
                now + constants.ACCURACY_REVIEW_CLAIM_SECONDS
            )
            schedule["updated_at"] = now
            self._persist(active=True)
            return (
                str(pending["review_id"]),
                str(pending.get("context") or ""),
            )


    def mark_accuracy_review_dispatched(self, review_id: str) -> bool:
        with self._lock:
            pending = (
                self.accuracy_schedule.get("pending_review")
                if isinstance(self.accuracy_schedule, dict)
                else None
            )
            if (
                not isinstance(pending, dict)
                or pending.get("review_id") != str(review_id)
            ):
                return False
            pending["phase"] = "dispatched"
            pending["claim_until"] = 0.0
            self.accuracy_schedule["updated_at"] = time.time()
            self._persist(active=True)
            return True


    def complete_accuracy_review(self, review_id: str) -> bool:
        with self._lock:
            schedule = self.accuracy_schedule
            pending = (
                schedule.get("pending_review")
                if isinstance(schedule, dict)
                else None
            )
            if (
                not isinstance(pending, dict)
                or pending.get("review_id") != str(review_id)
            ):
                return False
            schedule["next_checkpoint"] = max(
                int(schedule.get("next_checkpoint") or 1),
                int(pending.get("checkpoint_through") or 0) + 1,
            )
            schedule["pending_review"] = {}
            schedule["updated_at"] = time.time()
            self._persist(active=True)
            return True


    def accuracy_review_is_current(self, review_id: str) -> bool:
        with self._lock:
            pending = (
                self.accuracy_schedule.get("pending_review")
                if isinstance(self.accuracy_schedule, dict)
                else None
            )
            return bool(
                not self._closed
                and not self._terminal
                and isinstance(pending, dict)
                and pending.get("review_id") == str(review_id)
            )
