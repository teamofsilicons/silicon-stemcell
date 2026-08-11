"""Launching what has been waiting: the browser queue, and leases after a crash.

The drain holds the profile lock and the queue lock across the idle check, the
pop, and the launch — two managers must never both see an idle profile. It
publishes the work update *after* both locks release, because that call makes
an Interface request, and holding a cross-process lock across a network call is
how an instance wedges.

Reconciliation runs at boot: a worker that was alive when the process died
holds no lease, so one is adopted for it before anything can be called quiet.
"""
from __future__ import annotations


from helpers.state import file_lock
from worker import browser as browser_module
from worker import constants
from worker.leases import (
    _heartbeat_maintenance_activity,
)
from worker.process import (
    _launch_with_provider_order,
    _launch_worker_process,
)
from worker.registry import (
    _load_active,
    _load_browser_queue,
    _number_or_zero,
    _save_active,
    _save_browser_queue,
)

_sweep_call_counter = 0
_SWEEP_INTERVAL = 10



def reconcile_maintenance_activities():
    """Attach leases to legacy active/queued worker records before draining."""
    try:
        from manager.runtime.maintenance import COORDINATOR
    except Exception:
        return 0

    adopted = 0
    with file_lock(constants.ACTIVE_FILE):
        active = _load_active()
        changed = False
        for worker_id, info in active.items():
            if not isinstance(info, dict):
                continue
            reference = info.get("maintenance_activity") or {}
            if _heartbeat_maintenance_activity(reference) is not None:
                continue
            activity = COORDINATOR.adopt_prefence_activity(
                "worker",
                activity_id=str(worker_id),
                contact_id=str(info.get("carbon_id") or ""),
                started_at=_number_or_zero(info.get("started")),
            )
            if activity is not None:
                info["maintenance_activity"] = activity.reference()
                adopted += 1
                changed = True
        if changed:
            _save_active(active)

    with file_lock(constants.BROWSER_QUEUE_FILE):
        queue = _load_browser_queue()
        changed = False
        for info in queue:
            if not isinstance(info, dict):
                continue
            reference = info.get("maintenance_activity") or {}
            if _heartbeat_maintenance_activity(reference) is not None:
                continue
            activity = COORDINATOR.adopt_prefence_activity(
                "worker",
                activity_id=str(info.get("worker_id") or ""),
                contact_id=str(info.get("carbon_id") or ""),
                started_at=_number_or_zero(info.get("queued_at")),
            )
            if activity is not None:
                info["maintenance_activity"] = activity.reference()
                adopted += 1
                changed = True
        if changed:
            _save_browser_queue(queue)
    return adopted


def _process_browser_queue():
    # Keep the profiled-browser availability check, dequeue, and launch in one
    # cross-process transaction. Otherwise two manager threads can both see an
    # idle profile and launch competing sessions.
    with file_lock(constants.PROFILED_BROWSER_LOCK_FILE), file_lock(constants.BROWSER_QUEUE_FILE):
        if browser_module.profiled_browser_active():
            return None, None

        queue = _load_browser_queue()
        if not queue:
            return None, None

        next_job = queue[0]
        activity = _heartbeat_maintenance_activity(
            next_job.get("maintenance_activity")
        )
        try:
            from manager.runtime.maintenance import (
                acquire_descendant_activity,
                bind_activity,
                public_status,
            )

            phase = public_status().get("phase")
        except Exception:
            acquire_descendant_activity = None
            bind_activity = None
            phase = "available"

        # A legacy/unleased queued job cannot cross a newly raised fence.  It
        # remains durable and will be admitted when the runtime is available.
        if activity is None and phase != "available":
            return None, None
        if activity is None and acquire_descendant_activity is not None:
            activity = acquire_descendant_activity(
                "worker",
                activity_id=str(next_job.get("worker_id") or ""),
                contact_id=str(next_job.get("carbon_id") or ""),
            )
            if activity is None:
                return None, None
            next_job["maintenance_activity"] = activity.reference()

        queue.pop(0)
        _save_browser_queue(queue)

        scope = bind_activity(activity) if bind_activity is not None else None
        if scope is not None:
            scope.__enter__()
        try:
            if next_job.get("resume"):
                ok, result = _launch_worker_process(
                    next_job["worker_id"],
                    next_job["task"],
                    "browser",
                    next_job.get("carbon_id", "unknown"),
                    incognito=next_job.get("incognito", False),
                    resume=True,
                    provider=next_job.get("provider", "claude"),
                    session_id=next_job.get("session_id", ""),
                )
            else:
                ok, result = _launch_with_provider_order(
                    next_job["worker_id"],
                    next_job["task"],
                    "browser",
                    next_job.get("carbon_id", "unknown"),
                    incognito=next_job.get("incognito", False),
                    providers=next_job.get("providers"),
                )
        finally:
            if scope is not None:
                scope.__exit__(None, None, None)
        if not ok and activity is not None:
            try:
                from manager.runtime.maintenance import release_activity

                release_activity(activity)
            except Exception:
                pass
    try:
        from interface.work_updates import record_worker_state

        record_worker_state(
            next_job.get("carbon_id", "unknown"),
            next_job["worker_id"],
            "in_progress" if ok else "failed",
            "Browser worker is running" if ok else "Browser worker failed to launch",
        )
    except Exception:
        pass
    outcome = "Dequeued and started" if ok else "Dequeued but failed to start"
    return f"[Browser Queue] {outcome}: {result}", next_job.get("carbon_id", "unknown")
