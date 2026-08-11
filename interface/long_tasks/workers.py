"""The workers under a task: journalled before they start, reconciled after.
"""
from __future__ import annotations
from interface.work import workers as work_workers
from interface.long_tasks import constants
from interface.long_tasks import store as store_module
from interface.long_tasks import util as util_module
import time
from copy import deepcopy


class WorkerDeliveryMixin:
    def journal_worker_start(
        self,
        worker_id: str,
        worker_type: str,
        description: str,
        *,
        task_id: str = "",
    ) -> dict[str, str]:
        """Persist a non-publishable prepare record before launching a worker."""
        with self._lock:
            target_task_id = self.resolve_task_id(task_id) or self.task_id
            if (
                not target_task_id
                or (
                    worker_id not in self.pending_workers
                    and len(self.pending_workers) >= constants.MAX_PENDING_WORKERS
                )
            ):
                return {}
            invocation_id = util_module._stable_id(
                "worker-invocation",
                self.contact_id,
                self.run_id,
                worker_id,
            )
            group_id = util_module._stable_id(
                "worker-group", self.contact_id, target_task_id
            )
            self.pending_workers[str(worker_id)] = {
                "worker_id": str(worker_id),
                "worker_type": str(worker_type or "worker"),
                "description": util_module._compact(description, 500),
                "task_id": target_task_id,
                "group_id": group_id,
                "invocation_id": invocation_id,
                "phase": "prepared",
                "state": "yet_to_start",
                "state_description": "Preparing to launch",
                "attempts": 0,
                "next_attempt_at": 0.0,
                "prepared_at": time.time(),
                "fact_updated_at": time.time(),
            }
            self.worker_delivery_watermarks.pop(str(worker_id), None)
            self._persist(active=True)
            return {
                "task_id": target_task_id,
                "group_id": group_id,
                "invocation_id": invocation_id,
            }


    def mark_worker_started(
        self,
        worker_id: str,
        *,
        queued: bool,
    ) -> dict[str, str]:
        with self._lock:
            intent = self.pending_workers.get(str(worker_id))
            if not isinstance(intent, dict):
                return {}
            intent["phase"] = "launched"
            intent["state"] = "yet_to_start" if queued else "in_progress"
            intent["state_description"] = (
                "Queued and waiting to launch"
                if queued
                else f"{str(intent.get('worker_type') or 'worker').capitalize()} "
                "worker is running"
            )
            intent["next_attempt_at"] = 0.0
            intent["fact_updated_at"] = time.time()
            self._persist(active=True)
        delivered = self._deliver_pending_workers(force=True)
        if str(worker_id) not in delivered:
            delivered.update(self._deliver_pending_workers(force=True))
        return delivered.get(str(worker_id), {})


    def discard_worker_intent(self, worker_id: str) -> None:
        with self._lock:
            worker_id = str(worker_id)
            removed = self.pending_workers.pop(worker_id, None)
            if removed is not None:
                self.worker_delivery_watermarks[worker_id] = max(
                    time.time(),
                    float(
                        removed.get("fact_updated_at") or 0
                        if isinstance(removed, dict)
                        else 0
                    ),
                )
                self._persist(active=True)
        self.close_if_terminal()


    def record_pending_worker_state(
        self,
        worker_id: str,
        state_name: str,
        description: str = "",
    ) -> bool:
        with self._lock:
            intent = self.pending_workers.get(str(worker_id))
            if not isinstance(intent, dict):
                return False
            intent["phase"] = "launched"
            intent["state"] = str(state_name)
            if description:
                intent["state_description"] = util_module._compact(description, 500)
            intent["next_attempt_at"] = 0.0
            intent["fact_updated_at"] = time.time()
            self._persist(active=True)
            return True


    def _reconcile_worker_intents(
        self,
        *,
        force_prepared: bool = False,
    ) -> None:
        resolver = self.worker_status_resolver
        if resolver is None:
            return
        now = time.time()
        with self._lock:
            prepared = [
                str(worker_id)
                for worker_id, intent in self.pending_workers.items()
                if isinstance(intent, dict)
                and intent.get("phase") == "prepared"
                and (
                    force_prepared
                    or now - float(intent.get("prepared_at") or now)
                    >= constants.PREPARED_RECONCILE_GRACE_SECONDS
                )
            ]
        for worker_id in prepared:
            try:
                status = str(resolver(worker_id, self.contact_id) or "")
            except Exception:
                continue
            lowered = status.lower()
            if "not found" in lowered or "does not belong" in lowered:
                self.discard_worker_intent(worker_id)
                continue
            if "queued" in lowered:
                state, description = "yet_to_start", "Queued and waiting to launch"
            elif "running" in lowered or "is active" in lowered:
                state, description = "in_progress", "Worker is running"
            elif (
                "completed" in lowered
                or "is idle" in lowered
                or "archived run" in lowered
            ):
                state, description = "completed", "Worker completed"
            else:
                continue
            with self._lock:
                intent = self.pending_workers.get(worker_id)
                if not isinstance(intent, dict) or intent.get("phase") != "prepared":
                    continue
                intent["phase"] = "launched"
                intent["state"] = state
                intent["state_description"] = description
                intent["fact_updated_at"] = time.time()
                intent["next_attempt_at"] = 0.0
                self._persist(active=True)


    def _deliver_pending_workers(
        self,
        *,
        force: bool = False,
    ) -> dict[str, dict[str, str]]:
        delivered: dict[str, dict[str, str]] = {}
        try:
            with self._io_lock:
                with self._lock:
                    if not self.task_confirmed or self._terminal:
                        return delivered
                    self._merge_external_worker_facts_locked()
                    now = time.time()
                    intents = [
                        deepcopy(intent)
                        for intent in self.pending_workers.values()
                        if isinstance(intent, dict)
                        and intent.get("phase") in {"launched", "published"}
                        and (
                            force
                            or now
                            >= float(intent.get("next_attempt_at") or 0)
                        )
                    ]
                for intent in intents:
                    worker_id = str(intent.get("worker_id") or "")
                    if not worker_id:
                        continue
                    target_task_id = str(intent.get("task_id") or "")
                    with self._lock:
                        target_task_id = self.task_aliases.get(
                            target_task_id, target_task_id
                        )
                    if intent.get("phase") == "published":
                        state_delivered = work_workers.record_worker_state(
                            self.contact_id,
                            worker_id,
                            str(intent.get("state") or "in_progress"),
                            str(intent.get("state_description") or ""),
                        )
                        reference = (
                            deepcopy(
                                intent.get("published_reference")
                                or {
                                    "task_id": target_task_id,
                                    "group_id": str(
                                        intent.get("group_id") or ""
                                    ),
                                    "invocation_id": str(
                                        intent.get("invocation_id") or ""
                                    ),
                                }
                            )
                            if state_delivered
                            else {}
                        )
                    else:
                        reference = work_workers.record_worker_started(
                            self.contact_id,
                            worker_id,
                            str(intent.get("worker_type") or "worker"),
                            str(intent.get("description") or worker_id),
                            queued=intent.get("state") == "yet_to_start",
                            task_id=target_task_id,
                            invocation_id=str(
                                intent.get("invocation_id") or ""
                            ),
                            state_name=str(
                                intent.get("state") or "in_progress"
                            ),
                            state_description=str(
                                intent.get("state_description") or ""
                            ),
                        )
                    with self._lock:
                        self._merge_external_worker_facts_locked()
                        current = self.pending_workers.get(worker_id)
                        if (
                            not isinstance(current, dict)
                            or current.get("invocation_id")
                            != intent.get("invocation_id")
                        ):
                            continue
                        if reference:
                            if intent.get("phase") == "published":
                                current_updated = float(
                                    current.get("fact_updated_at") or 0
                                )
                                sent_updated = float(
                                    intent.get("fact_updated_at") or 0
                                )
                                if current_updated > sent_updated:
                                    current["phase"] = "published"
                                    current["published_reference"] = deepcopy(
                                        reference
                                    )
                                    current["attempts"] = 0
                                    current["next_attempt_at"] = 0.0
                                else:
                                    self.pending_workers.pop(worker_id, None)
                                    self.worker_delivery_watermarks[
                                        worker_id
                                    ] = max(time.time(), current_updated)
                                    delivered[worker_id] = reference
                            else:
                                current_updated = float(
                                    current.get("fact_updated_at") or 0
                                )
                                sent_updated = float(
                                    intent.get("fact_updated_at") or 0
                                )
                                if current_updated > sent_updated:
                                    # A real worker fact raced the initial
                                    # create. Keep the accepted correlation
                                    # until that newer fact is delivered.
                                    current["phase"] = "published"
                                    current["published_reference"] = deepcopy(
                                        reference
                                    )
                                    current["attempts"] = 0
                                    current["next_attempt_at"] = 0.0
                                else:
                                    # The accepted create already carried the
                                    # latest known state, so there is no
                                    # second mutation to replay.
                                    self.pending_workers.pop(worker_id, None)
                                    self.worker_delivery_watermarks[
                                        worker_id
                                    ] = max(time.time(), current_updated)
                                    delivered[worker_id] = reference
                        else:
                            attempts = int(current.get("attempts") or 0) + 1
                            current["attempts"] = attempts
                            current["next_attempt_at"] = util_module._retry_at(attempts)
                        self._persist(active=True)
        finally:
            # Terminal observation can race the last delivery. Re-evaluate
            # only after the IO/worker locks are released.
            self.close_if_terminal()
        return delivered


    def _merge_external_worker_facts_locked(self) -> None:
        entry = store_module._state_entry(self.contact_id)
        external_workers = entry.get("pending_workers")
        if not isinstance(external_workers, dict):
            return
        for worker_id, external in external_workers.items():
            if not isinstance(external, dict):
                continue
            local = self.pending_workers.get(str(worker_id))
            external_updated = float(external.get("fact_updated_at") or 0)
            watermark = float(
                self.worker_delivery_watermarks.get(str(worker_id)) or 0
            )
            if external_updated <= watermark:
                continue
            if not isinstance(local, dict) or external_updated > float(
                local.get("fact_updated_at") or 0
            ):
                merged = deepcopy(external)
                if not isinstance(local, dict):
                    merged["phase"] = "published"
                elif local.get("phase") == "published":
                    merged["phase"] = "published"
                    merged["published_reference"] = deepcopy(
                        local.get("published_reference") or {}
                    )
                self.pending_workers[str(worker_id)] = merged
