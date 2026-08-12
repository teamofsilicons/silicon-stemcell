"""What a manager can ask about its workers.

Start one, message a running one, stop one, ask how it is going, list what is
active or archived. The mechanics live in :mod:`worker.process`,
:mod:`worker.registry`, :mod:`worker.dispatch` and :mod:`worker.sweep`.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

from helpers.state import file_lock
from worker import browser as browser_module
from worker import constants
from worker.base import Worker
from worker.constants import IS_WINDOWS
from worker.leases import (
    _release_maintenance_activity,
)
from worker.process import (
    _sync_session_id,
)
from worker.registry import (
    _archive_active_output,
    _create_worker_record,
    _get_worker_record,
    _load_active,
    _load_archive_meta,
    _load_browser_queue,
    _remove_active_worker,
    _remove_worker_record,
    _save_browser_queue,
    _update_worker_record,
)

_sweep_call_counter = 0
_SWEEP_INTERVAL = 10



def start_worker(worker_id, task, worker_type, carbon_id, incognito=False):
    if not worker_type:
        return "Error: worker_type is required. Available types: browser, terminal, writer"

    worker_type = worker_type.lower()
    try:
        from manager.runtime.maintenance import (
            acquire_descendant_activity,
            bind_activity,
            release_activity,
        )

        activity = acquire_descendant_activity(
            "worker",
            activity_id=worker_id,
            contact_id=carbon_id,
        )
    except Exception:
        activity = None
        bind_activity = None
        release_activity = None
    if activity is None:
        return (
            "Error: Silicon is preparing an update. New workers are paused; "
            "the manager task will resume after maintenance."
        )

    result = ""
    scope = bind_activity(activity)
    scope.__enter__()
    try:
        _, err = _create_worker_record(
            worker_id,
            worker_type,
            carbon_id,
            incognito=incognito,
        )
        if err:
            result = err
        elif Worker.resolve(worker_type, worker_id, carbon_id) is not None:
            worker = Worker.resolve(worker_type, worker_id, carbon_id)
            result = worker.start(task, resume=False, incognito=incognito)
            # Terminal drops its own record on launch failure; the other two
            # forget theirs on any refusal, including "already active".
            if result.startswith("Error:") and worker.forget_record_on_start_error:
                _remove_worker_record(worker_id)
        else:
            _remove_worker_record(worker_id)
            result = (
                f"Error: invalid worker_type '{worker_type}'. "
                "Available types: browser, terminal, writer"
            )
    finally:
        scope.__exit__(None, None, None)

    if result.startswith("Error:") and release_activity is not None:
        release_activity(activity)
    return result


def message_worker(worker_id, task, carbon_id):
    worker_record = _get_worker_record(worker_id)
    if not worker_record:
        return f"Error: Worker '{worker_id}' does not exist. Create it first with worker/new."
    if worker_record.get("carbon_id") != carbon_id:
        return f"Error: Worker '{worker_id}' does not belong to you."

    worker_type = worker_record.get("worker_type", "").lower()
    incognito = worker_record.get("incognito", False)

    active = _load_active()
    if worker_id in active:
        return f"Error: Worker '{worker_id}' is already active."

    queue = _load_browser_queue()
    if any(q["worker_id"] == worker_id for q in queue):
        return f"Error: Worker '{worker_id}' is already in the browser queue."

    try:
        from manager.runtime.maintenance import (
            acquire_descendant_activity,
            bind_activity,
            release_activity,
        )

        activity = acquire_descendant_activity(
            "worker",
            activity_id=worker_id,
            contact_id=carbon_id,
        )
    except Exception:
        activity = None
        bind_activity = None
        release_activity = None
    if activity is None:
        return (
            "Error: Silicon is preparing an update. Worker resumes are paused "
            "until maintenance finishes."
        )

    scope = bind_activity(activity)
    scope.__enter__()
    try:
        worker = Worker.resolve(worker_type, worker_id, carbon_id)
        if worker is not None:
            result = worker.start(task, resume=True, incognito=incognito)
        else:
            result = f"Error: Worker '{worker_id}' has invalid worker_type '{worker_type}'."
    finally:
        scope.__exit__(None, None, None)
    if result.startswith("Error:") and release_activity is not None:
        release_activity(activity)
    return result


def _is_process_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_worker_status(worker_id, carbon_id):
    worker_record = _get_worker_record(worker_id)
    if not worker_record:
        return f"Error: Worker '{worker_id}' not found."
    if worker_record.get("carbon_id") != carbon_id:
        return f"Error: Worker '{worker_id}' does not belong to you."

    queue = _load_browser_queue()
    for i, q in enumerate(queue):
        if q["worker_id"] == worker_id:
            return f"Worker '{worker_id}' status: queued (position {i+1} in browser queue)"

    active = _load_active()
    if worker_id not in active:
        archive_id = worker_record.get("last_archive_id", "")
        if archive_id:
            provider = worker_record.get("provider", "unknown")
            return f"Worker '{worker_id}' is idle ({provider}). Last archived run: {archive_id}"
        return f"Worker '{worker_id}' is idle. No archived runs yet."

    worker_info = active[worker_id]
    if _sync_session_id(worker_id, worker_info):
        worker_info = _load_active().get(worker_id, worker_info)

    output_path = worker_info.get("output_path")
    if not output_path or not os.path.exists(output_path):
        return f"Worker '{worker_id}' is active, but its output file is missing."

    with open(output_path) as f:
        raw = f.read()

    parsed = _parse_worker_output(raw, worker_info.get("provider", "claude"))
    worker_type = worker_info.get("worker_type", "unknown")
    provider = worker_info.get("provider", "unknown")
    status = "running" if _is_process_running(worker_info.get("pid")) else "completed"
    return f"Worker '{worker_id}' ({worker_type}, {provider}) status: {status}\n\nOutput so far:\n{parsed}"


def stop_worker(worker_id, carbon_id):
    found_in_queue = False
    queued_activity = {}
    with file_lock(constants.BROWSER_QUEUE_FILE):
        queue = _load_browser_queue()
        for q in queue:
            if q["worker_id"] == worker_id:
                if q.get("carbon_id") != carbon_id:
                    return f"Error: Worker '{worker_id}' does not belong to you."
                found_in_queue = True
                queued_activity = q.get("maintenance_activity") or {}
                break
        if found_in_queue:
            _save_browser_queue(
                [q for q in queue if q["worker_id"] != worker_id]
            )

    if found_in_queue:
        _release_maintenance_activity(queued_activity)
        try:
            from interface.cron.checkback import remove_checkback
            remove_checkback(worker_id)
        except Exception:
            pass
        try:
            from interface.work import record_worker_state

            record_worker_state(
                carbon_id,
                worker_id,
                "cancelled",
                "Worker was removed from the queue",
            )
        except Exception:
            pass
        return f"Done. Worker '{worker_id}' removed from browser queue."

    with file_lock(constants.ACTIVE_FILE):
        active = _load_active()
        if worker_id not in active:
            return f"Error: Worker '{worker_id}' is not active."
        if active[worker_id].get("carbon_id") != carbon_id:
            return f"Error: Worker '{worker_id}' does not belong to you."

        worker_info = active[worker_id]
        if _sync_session_id(worker_id, worker_info):
            worker_info = _load_active().get(worker_id, worker_info)

    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(worker_info["pid"])], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(worker_info["pid"]), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass

    browser_module._cleanup_silicon_browser_session(worker_id, worker_info)

    _remove_active_worker(worker_id, worker_info.get("run_id", ""))
    _release_maintenance_activity(worker_info.get("maintenance_activity") or {})

    try:
        from interface.cron.checkback import remove_checkback
        remove_checkback(worker_id)
    except Exception:
        pass
    try:
        from interface.work import record_worker_state

        record_worker_state(
            carbon_id,
            worker_id,
            "cancelled",
            "Worker was stopped",
        )
    except Exception:
        pass

    archive_id = _archive_active_output(worker_id, worker_info, carbon_id)
    queue_result, queue_carbon_id = _drain_browser_queue()
    if archive_id:
        _update_worker_record(worker_id, last_archive_id=archive_id, last_used_at=time.time())
        suffix = f" {queue_result}" if queue_result and queue_carbon_id == carbon_id else ""
        return f"Done. Worker '{worker_id}' stopped. Output archived as '{archive_id}'{suffix}"

    suffix = f" {queue_result}" if queue_result and queue_carbon_id == carbon_id else ""
    return f"Done. Worker '{worker_id}' stopped.{suffix}"


def list_active(carbon_id):
    active = _load_active()
    queue = _load_browser_queue()

    my_active = {wid: info for wid, info in active.items() if info.get("carbon_id") == carbon_id}
    my_queue = [q for q in queue if q.get("carbon_id") == carbon_id]

    if not my_active and not my_queue:
        return "No active or queued workers."

    lines = []
    if my_active:
        for wid, info in my_active.items():
            elapsed = time.time() - info["started"]
            minutes = int(elapsed // 60)
            wtype = info.get("worker_type", "unknown")
            provider = info.get("provider", "unknown")
            lines.append(f"- {wid} ({wtype}, {provider}, pid: {info['pid']}, running for {minutes}m, task: {info['task'][:80]})")

    if my_queue:
        lines.append("")
        lines.append("Browser queue (your position in global queue):")
        for i, q in enumerate(queue):
            if q.get("carbon_id") == carbon_id:
                lines.append(f"  position {i+1}. {q['worker_id']} (task: {q['task'][:80]})")

    return "Active workers:\n" + "\n".join(lines)


def list_archive(carbon_id):
    if not os.path.exists(constants.OUTPUTS_DIR):
        return "No archives."

    meta = _load_archive_meta()
    archives = []
    for archive_id, info in meta.items():
        if info.get("carbon_id") == carbon_id:
            fpath = os.path.join(constants.OUTPUTS_DIR, f"{archive_id}.log")
            if os.path.exists(fpath):
                provider = info.get("provider", "unknown")
                archives.append(f"- {archive_id} ({provider})")

    if not archives:
        return "No archives."

    return "Your archived workers:\n" + "\n".join(sorted(archives))


def read_archive(archive_id, carbon_id):
    meta = _load_archive_meta()
    archive_info = meta.get(archive_id)

    if archive_info and archive_info.get("carbon_id") != carbon_id:
        return f"Error: Archive '{archive_id}' does not belong to you."

    archive_path = os.path.join(constants.OUTPUTS_DIR, f"{archive_id}.log")
    if not os.path.exists(archive_path):
        return f"Error: Archive '{archive_id}' not found."

    with open(archive_path) as f:
        raw = f.read()

    provider = archive_info.get("provider", "claude") if archive_info else "claude"
    return _parse_worker_output(raw, provider)


def _drain_browser_queue():
    """Launch whatever was waiting on the browser profile, if anything is."""
    from worker.dispatch import _process_browser_queue

    return _process_browser_queue()


def _parse_worker_output(raw, provider="claude"):
    from worker.sweep import _parse_worker_output as parse

    return parse(raw, provider)
