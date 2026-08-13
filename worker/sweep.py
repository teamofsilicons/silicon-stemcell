"""Noticing that a worker has finished, exactly once.

A finished worker is claimed with a token before anything is delivered, and a
second sweep will not re-claim it — a completion delivered twice is a manager
told twice that its work is done.

Each completed run also gets a diagnostics child run linked back to the message
that spawned it, so a worker's provider events sit under the turn that asked
for them.
"""
from __future__ import annotations

import os
import time

from inference import json_events
from worker import browser as browser_module
from worker.leases import (
    _heartbeat_maintenance_activity,
    _release_maintenance_activity,
)
from worker.process import (
    _read_text_file,
    _sync_session_id,
    _worker_provider,
)
from worker.registry import (
    _archive_active_output,
    _claim_completed_worker,
    _load_active,
    _load_browser_queue,
    _remove_active_worker,
    _update_worker_record,
)

_sweep_call_counter = 0
_SWEEP_INTERVAL = 10



def _has_completion_event(output_path, provider):
    if not output_path or not os.path.exists(output_path):
        return False
    raw = _read_text_file(output_path)
    return _worker_provider(provider).has_completion_event(raw)


def _worker_terminal_state(raw, provider):
    """Return the real terminal state without publishing raw worker output."""
    return _worker_provider(provider).terminal_state(raw)


def _worker_completion_context(completion):
    return (
        f"Worker '{completion['worker_id']}' "
        f"({completion['worker_type']}, {completion['provider']}) completed:\n"
        f"Result: {completion['result']}\n"
        "Complete worker output archived with id: "
        f"{completion['archive_id']}"
    )


def _record_worker_diagnostics(worker_id, worker_info, carbon_id, provider, output_path):
    """Create a child run linked back to every message that spawned the worker."""
    try:
        from diagnostics.store import Diagnostics
        from interface.progress import DONE, usage_from_done_event
        parent_run_id = worker_info.get("diag_parent_run_id")
        if not parent_run_id:
            return
        child_trace = Diagnostics.start_run(
            trigger="worker",
            carbon_id=carbon_id,
            parent_run_id=parent_run_id,
            room_id=worker_info.get("diag_room_id", ""),
            message_ids=worker_info.get("diag_message_ids") or [],
            meta={"worker_id": worker_id},
        )
        raw = _read_text_file(output_path)
        events = json_events(raw)
        state = {}
        done_events = []
        with child_trace.span("worker.execution") as worker_span:
            worker_span.set_meta(
                worker_id=worker_id,
                worker_type=worker_info.get("worker_type", "unknown"),
                provider=provider,
                worker_run_id=worker_info.get("run_id", ""),
                started_at_ms=int(float(worker_info.get("started") or time.time()) * 1000),
            )
            with child_trace.span("provider_call") as provider_span:
                provider_span.set_meta(provider=provider, worker_id=worker_id)
                sequence = 0
                engine = _worker_provider(provider)
                for event in events:
                    for item in engine.progress_events(event, state):
                        sequence += 1
                        child_trace.event(
                            "worker.progress",
                            sequence=sequence,
                            kind=str(item.get("kind") or ""),
                            status=str(item.get("status") or ""),
                            item_id=str(item.get("item_id") or ""),
                            tool_name=str(item.get("tool_name") or ""),
                            path=str(item.get("path") or "")[:500],
                            query=str(item.get("query") or "")[:500],
                            command=str(item.get("command") or "")[:500],
                        )
                        if item.get("kind") == DONE:
                            done_events.append(item)
                for item in done_events:
                    provider_span.set_tokens(**usage_from_done_event(item))
        child_trace.close()
    except Exception:
        pass


def check_completed_workers():
    global _sweep_call_counter
    _sweep_call_counter += 1
    if _sweep_call_counter >= _SWEEP_INTERVAL:
        _sweep_call_counter = 0
        browser_module.sweep_orphaned_daemons()

    # Worker subprocesses cannot update the coordinator themselves. The
    # runtime sweep heartbeats both active and profiled-browser queued work so
    # a long accepted task remains a hard blocker for maintenance.
    for queued in _load_browser_queue():
        if isinstance(queued, dict):
            _heartbeat_maintenance_activity(
                queued.get("maintenance_activity") or {}
            )

    active = _load_active()
    for worker_info in active.values():
        if isinstance(worker_info, dict):
            _heartbeat_maintenance_activity(
                worker_info.get("maintenance_activity") or {}
            )
    completed_by_carbon = {}

    for worker_id, initial_info in list(active.items()):
        worker_info = initial_info
        provider = worker_info.get("provider", "claude")
        output_path = worker_info.get("output_path")

        if _sync_session_id(worker_id, worker_info):
            worker_info = _load_active().get(worker_id, worker_info)
            output_path = worker_info.get("output_path")

        process_running = _is_process_running(worker_info.get("pid"))
        if process_running and not _has_completion_event(output_path, provider):
            continue

        worker_info = _claim_completed_worker(
            worker_id,
            worker_info.get("run_id", ""),
        )
        if not worker_info:
            continue

        worker_type = worker_info.get("worker_type", "unknown")
        carbon_id = worker_info.get("carbon_id", "unknown")
        _record_worker_diagnostics(worker_id, worker_info, carbon_id, provider, output_path)

        browser_module._cleanup_silicon_browser_session(worker_id, worker_info)

        raw = _read_text_file(output_path)
        result_text = _parse_worker_output(raw, provider)
        terminal_state = _worker_terminal_state(raw, provider)
        try:
            from interface.work import record_worker_state

            record_worker_state(
                carbon_id,
                worker_id,
                terminal_state,
                (
                    "Worker completed"
                    if terminal_state == "completed"
                    else "Worker execution failed"
                ),
            )
        except Exception:
            pass
        archive_id = _archive_active_output(worker_id, worker_info, carbon_id)
        if archive_id:
            _update_worker_record(worker_id, last_archive_id=archive_id, last_used_at=time.time())

        completion = {
            "worker_id": worker_id,
            "worker_type": worker_type,
            "provider": provider,
            "carbon_id": carbon_id,
            "archive_id": archive_id,
            "result": result_text,
        }
        activity_reference = worker_info.get("maintenance_activity") or {}
        continuation_queued = False
        if activity_reference:
            try:
                from silicon.runtime.maintenance import COORDINATOR

                continuation_queued = COORDINATOR.enqueue_continuation(
                    carbon_id,
                    _worker_completion_context(completion),
                    activity_reference,
                )
            except Exception:
                continuation_queued = False

        _remove_active_worker(
            worker_id,
            worker_info.get("run_id", ""),
            worker_info.get("_completion_claim_token", ""),
        )
        if not continuation_queued:
            _release_maintenance_activity(activity_reference)

        try:
            from interface.cron.checkback import remove_checkback
            remove_checkback(worker_id)
        except Exception:
            pass

        completion["maintenance_continuation_queued"] = continuation_queued
        completed_by_carbon.setdefault(carbon_id, []).append(completion)

    queue_result, queue_carbon_id = _drain_browser_queue()
    if queue_result and queue_carbon_id:
        completed_by_carbon.setdefault(queue_carbon_id, []).append({
            "worker_id": "[queue]",
            "worker_type": "browser",
            "provider": "",
            "carbon_id": queue_carbon_id,
            "archive_id": "",
            "result": queue_result,
        })

    return completed_by_carbon


def check_completed_workers_formatted():
    completed_by_carbon = check_completed_workers()
    if not completed_by_carbon:
        return {}

    result = {}
    for carbon_id, completed in completed_by_carbon.items():
        parts = []
        for c in completed:
            if c["worker_id"] == "[queue]":
                parts.append(c["result"])
            elif c.get("maintenance_continuation_queued"):
                # The durable maintenance queue now owns this accepted
                # continuation, including during a drain.
                continue
            else:
                parts.append(_worker_completion_context(c))
        if parts:
            result[carbon_id] = "\n\n".join(parts)
    return result


def _parse_worker_output(raw, provider="claude"):
    """The provider's own reading of what a worker produced."""
    return _worker_provider(provider).parse_output(raw)


def _is_process_running(pid):
    from worker.pool import _is_process_running as running

    return running(pid)


def _drain_browser_queue():
    from worker.dispatch import _process_browser_queue

    return _process_browser_queue()
