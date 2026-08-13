"""Serializing turns, and landing a message in one that is already running.

There is one session, so there is one turn at a time. A message that arrives
while it is running is injected into it rather than queued behind it, which is
why a message lands as soon as it is sent even mid-task. The keying below is
still generic — the queue does not care that today every root arrives under
:data:`helpers.session.SILICON`.
"""
from interface import long_tasks as long_tasks_module
from silicon.turn import run_all_managers
import os
import threading
import time
from helpers.session import also_answering, answering, origins_in
from silicon import (
    INJECTED_PREFIX,
)
from interface import (
    maintenance_inbox_quiescent,
    schedule_maintenance_notices,
    start_listener,
    stop_listener,
)
from iwantto import injection
from diagnostics import journal as iwantto_journal
from silicon.runtime.maintenance import (
    COORDINATOR as MAINTENANCE,
    RootAdmission,
    heartbeat_scope,
)
from worker import (
    reconcile_maintenance_activities,
)
from interface.long_tasks import (
    acknowledge_accuracy_review_dispatched,
    claim_ready_accuracy_review_roots,
    claim_ready_long_task_roots,
    complete_accuracy_review_root,

)

from diagnostics.logs import runtime_log as log


class ManagerDispatcher:
    """Serialize turns, coalescing anything that arrives while one is running.

    Interface ingestion stays live while the session and its workers are busy.
    A new message reaches the running turn through
    :mod:`iwantto.injection`, and only falls back to the next turn if that turn
    has stopped accepting.
    """

    def __init__(self, runner=None, *, max_active_contacts=16):
        self._runner = runner or run_all_managers
        self._condition = threading.Condition()
        self._pending = {}
        # Roots handed to a turn already in flight; completed with it.
        self._injected = {}
        self._running = set()
        self._threads = set()
        self._closed = False
        self._slots = threading.BoundedSemaphore(max(1, int(max_active_contacts)))

    def submit(self, context_by_carbon):
        """Durably enqueue roots and start only those admitted before the fence."""
        admissions = []
        transferred_accuracy_reviews = []
        for carbon_id, context in (context_by_carbon or {}).items():
            if not context:
                continue
            result = MAINTENANCE.enqueue_root(carbon_id, str(context))
            accuracy_review_id, _ = long_tasks_module.extract_accuracy_review_root(str(context))
            if accuracy_review_id:
                transferred_accuracy_reviews.append(
                    (str(carbon_id), accuracy_review_id)
                )
            if result.admission is not None:
                admissions.append(result.admission)
        self._schedule_admissions(admissions)
        # Keep a queued long-task head and its lease intact until
        # run_all_managers crosses the lifecycle fence.  Maintenance owns the
        # admission retry, but acknowledging here would also erase the only
        # FIFO authority that permits the claimed head to launch.
        for contact_id, review_id in transferred_accuracy_reviews:
            try:
                acknowledge_accuracy_review_dispatched(
                    contact_id,
                    review_id,
                )
            except Exception as exc:
                log(
                    "[Silicon] Accuracy-review acknowledgement deferred: "
                    f"{type(exc).__name__}"
                )

    def _inject_into_live_run(self, admission):
        """Hand a newly-arrived root to the turn that is already running.

        Durability is unchanged: the root was enqueued before this, and it is
        completed (or retried) alongside the batch it was injected into, so it
        shares that run's fate rather than being trusted to a process that has
        not finished yet.
        """
        carbon_id = admission.contact_id
        accepted = injection.offer(
            injection.MANAGER,
            carbon_id,
            INJECTED_PREFIX + str(admission.context),
        )
        if not accepted:
            return False
        # Whoever sent it is part of what this turn is answering from now on, so
        # its progress and work frames reach their room too.
        also_answering(origins_in(admission.context))
        self._injected.setdefault(carbon_id, []).append(admission)
        log(f"[Silicon] Injected a new message into the live run for {carbon_id}.")
        try:
            iwantto_journal.record_message(
                "in", carbon_id, via="injected", body=str(admission.context)
            )
        except Exception:
            pass
        return True

    def _take_injected(self, carbon_id):
        with self._condition:
            return self._injected.pop(carbon_id, [])

    def _schedule_admissions(self, admissions):
        started = []
        with self._condition:
            if self._closed:
                raise RuntimeError("manager dispatcher is closed")
            for admission in admissions:
                if not isinstance(admission, RootAdmission):
                    continue
                carbon_id = admission.contact_id
                # A contact that is mid-turn can take the message now instead
                # of waiting for the whole run to finish.
                if carbon_id in self._running and self._inject_into_live_run(
                    admission
                ):
                    continue
                self._pending.setdefault(carbon_id, []).append(admission)
                if carbon_id in self._running:
                    continue
                self._running.add(carbon_id)
                thread = threading.Thread(
                    target=self._run_contact,
                    args=(carbon_id,),
                    name=f"manager-dispatch-{carbon_id}",
                    daemon=True,
                )
                self._threads.add(thread)
                started.append(thread)
            self._condition.notify_all()
        for thread in started:
            thread.start()

    def replay_maintenance_queue(self, *, limit=100):
        """Claim durable roots after a cancelled/completed maintenance window."""
        admissions = MAINTENANCE.claim_pending_roots(limit=limit)
        if admissions:
            self._schedule_admissions(admissions)
        return len(admissions)

    def _run_contact(self, carbon_id):
        released = False
        try:
            with self._slots:
                while True:
                    with self._condition:
                        admissions = self._pending.pop(carbon_id, [])
                        if not admissions:
                            stranded = self._injected.pop(carbon_id, [])
                            if stranded:
                                # Their run finished before this thread exited.
                                MAINTENANCE.complete_roots(stranded)
                            self._running.discard(carbon_id)
                            self._threads.discard(threading.current_thread())
                            released = True
                            self._condition.notify_all()
                            return
                    batches = []
                    normal_batch = []
                    for admission in admissions:
                        review_id, _ = long_tasks_module.extract_accuracy_review_root(
                            admission.context
                        )
                        if review_id:
                            if normal_batch:
                                batches.append(normal_batch)
                                normal_batch = []
                            batches.append([admission])
                        else:
                            normal_batch.append(admission)
                    if normal_batch:
                        batches.append(normal_batch)

                    for batch in batches:
                        try:
                            context = "\n\n".join(
                                item.context for item in batch
                            )
                            # Internal accuracy reviews stay isolated from
                            # user roots; every admission remains leased until
                            # its own manager turn actually returns.
                            with heartbeat_scope(
                                [item.activity for item in batch],
                                coordinator=MAINTENANCE,
                            ), answering(origins_in(context)):
                                self._runner({carbon_id: context})
                            for item in batch:
                                review_id, _ = long_tasks_module.extract_accuracy_review_root(
                                    item.context
                                )
                                if review_id:
                                    complete_accuracy_review_root(
                                        carbon_id,
                                        review_id,
                                    )
                            # Anything injected while that runner was going
                            # was handled by it, so it completes with it.
                            MAINTENANCE.complete_roots(
                                batch + self._take_injected(carbon_id)
                            )
                            log(
                                "[Silicon] Manager loop complete for "
                                f"{carbon_id}."
                            )
                        except Exception as exc:
                            MAINTENANCE.retry_roots(
                                batch + self._take_injected(carbon_id)
                            )
                            log(
                                "[Silicon] Manager dispatcher error for "
                                f"{carbon_id}: {exc}"
                            )
        finally:
            if not released:
                with self._condition:
                    self._running.discard(carbon_id)
                    self._threads.discard(threading.current_thread())
                    self._condition.notify_all()

    def wait_for_idle(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        with self._condition:
            while self._running or any(self._pending.values()):
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def shutdown(self, *, wait=False):
        with self._condition:
            self._closed = True
            threads = list(self._threads)
            self._condition.notify_all()
        if wait:
            for thread in threads:
                thread.join()


def _maintenance_runtime_tick(dispatcher, *, attest=True):
    """Replay released work, publish acknowledgements, and attest quiescence."""
    try:
        if MAINTENANCE.public_status()["phase"] == "available":
            start_listener()
        else:
            stop_listener()
    except Exception as exc:
        log(f"[Silicon] Maintenance listener fence deferred: {exc}")

    try:
        reconcile_maintenance_activities()
    except Exception as exc:
        log(f"[Silicon] Maintenance worker reconciliation deferred: {exc}")

    try:
        dispatcher.replay_maintenance_queue()
    except Exception as exc:
        log(f"[Silicon] Maintenance replay deferred: {exc}")

    try:
        if MAINTENANCE.public_status().get("pending_notice_count"):
            schedule_maintenance_notices()
    except Exception as exc:
        log(f"[Silicon] Maintenance acknowledgement deferred: {exc}")

    try:
        status = MAINTENANCE.public_status()
        if (
            attest
            and status["phase"] == "draining"
            and status["active_count"] == 0
            and dispatcher.wait_for_idle(timeout=0)
        ):
            from helpers.process import flush_best_effort

            flushed = flush_best_effort(timeout=0.25)
            MAINTENANCE.acknowledge_runtime_quiescent(
                epoch=status["epoch"],
                outbox_flushed=flushed and maintenance_inbox_quiescent(),
                pid=os.getpid(),
            )
    except Exception as exc:
        log(f"[Silicon] Maintenance quiescence check deferred: {exc}")
    try:
        return MAINTENANCE.public_status()
    except Exception:
        return {"phase": "available"}


def _merge_due_internal_roots(
    context_by_carbon,
    *,
    maintenance_active,
):
    """Add lifecycle roots without mixing accuracy reviews into user turns."""
    merged = dict(context_by_carbon or {})
    if maintenance_active:
        return merged

    queued_long_task_roots = claim_ready_long_task_roots(limit=16)
    for contact_id, queued_context in queued_long_task_roots.items():
        if contact_id in merged:
            merged[contact_id] = (
                f"{queued_context}\n\n{merged[contact_id]}"
            )
        else:
            merged[contact_id] = queued_context

    # Accuracy reviews are deliberately isolated from user and queued-root
    # turns. ManagerDispatcher independently preserves this after admission.
    accuracy_roots = claim_ready_accuracy_review_roots(
        limit=16,
        exclude_contacts={str(contact_id) for contact_id in merged},
    )
    merged.update(accuracy_roots)
    return merged
