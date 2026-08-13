"""Spawning, messaging and checking back on workers."""
from interface.long_tasks import registry as lt_registry
import worker as worker_module
from silicon.tools.base import register
from silicon.tools.helpers import _parse_worker_tool
from silicon.tools.helpers import _work_reference_suffix
from worker import (
    start_worker,
    message_worker,

    stop_worker,
    list_active,
    list_archive,
    read_archive,
)
from interface.cron.checkback import add_checkback
from diagnostics.store import Diagnostics
from interface.work import (
    record_worker_started,
)


def _worker_new(tool_spec, carbon_id, worker_type, worker_id):
    """Spawn a worker, journalling the intent first so a crash can't lose it."""
    if not worker_type:
        return "Tool 'worker/new': Error: worker_type is required. Use worker/browser, worker/terminal, or worker/writer"
    if not worker_id:
        return "Tool 'worker/new': Error: worker-id is required"
    task = tool_spec.get("task", "")
    if not task:
        return f"Tool 'worker/new' ({worker_id}): Error: task is required"

    lifecycle = lt_registry.current_long_task(carbon_id)
    lifecycle_task_id = ""
    pending_work_invocation = {}
    if lifecycle is not None:
        lifecycle_task_id = lifecycle.ensure("spawning_worker")
        durable_task_id = lifecycle.resolve_task_id(
            str(tool_spec.get("task_id") or "")
        )
        if durable_task_id:
            # Refuse to start the worker if we cannot durably record that we did.
            pending_work_invocation = lifecycle.journal_worker_start(
                worker_id,
                worker_type,
                task,
                task_id=durable_task_id,
            )
            if not pending_work_invocation:
                return (
                    f"Tool 'worker/new' ({worker_type}, {worker_id}): "
                    "Error: durable worker update admission is "
                    "unavailable; worker was not started"
                )

    incognito = tool_spec.get("incognito", False)
    status = start_worker(worker_id, task, worker_type, carbon_id, incognito=incognito)
    work_invocation = {}
    if "Error" not in status:
        if lifecycle is not None and pending_work_invocation:
            work_invocation = lifecycle.mark_worker_started(
                worker_id,
                queued="queued" in status.lower(),
            )
            if not work_invocation:
                status += " (durable worker update queued for retry)"
        else:
            work_invocation = record_worker_started(
                carbon_id,
                worker_id,
                worker_type,
                task,
                queued="queued" in status.lower(),
                task_id=str(
                    (
                        lifecycle.resolve_task_id(
                            str(tool_spec.get("task_id") or "")
                        )
                        if lifecycle is not None
                        else tool_spec.get("task_id")
                    )
                    or lifecycle_task_id
                    or ""
                ),
            )
        trace = Diagnostics.get_active_run(carbon_id)
        if trace is not None:
            trace.note_worker_spawned()
            trace.event("worker.spawned", worker_id=worker_id, worker_type=worker_type)
    elif lifecycle is not None and pending_work_invocation:
        lifecycle.discard_worker_intent(worker_id)

    checkback_in = tool_spec.get("checkback_in")
    if checkback_in and "Error" not in status:
        try:
            add_checkback(worker_id, carbon_id, float(checkback_in))
            status += f" (checkback in {checkback_in} min)"
        except Exception as e:
            status += f" (checkback setup failed: {e})"

    return (
        f"Tool 'worker/new' ({worker_type}, {worker_id}): {status}"
        + _work_reference_suffix(
            work_invocation,
            "task_id",
            "group_id",
            "invocation_id",
        )
    )


def _worker_message(tool_spec, carbon_id, worker_id):
    """Send follow-up instructions to a worker that is already running."""
    task = tool_spec.get("message", "")
    if not worker_id:
        return "Tool 'worker/message': Error: worker-id is required"
    if not task:
        return f"Tool 'worker/message' ({worker_id}): Error: message is required"
    status = message_worker(worker_id, task, carbon_id)
    work_invocation = {}
    if "Error" not in status:
        work_invocation = record_worker_started(
            carbon_id,
            worker_id,
            "worker",
            task,
            queued="queued" in status.lower(),
            task_id=str(tool_spec.get("task_id") or ""),
        )
    return (
        f"Tool 'worker/message' ({worker_id}): {status}"
        + _work_reference_suffix(
            work_invocation,
            "task_id",
            "group_id",
            "invocation_id",
        )
    )


def _worker_checkback(tool_spec, carbon_id, worker_id):
    """Schedule a reminder to look in on a worker after N minutes."""
    checkback_in = tool_spec.get("checkback_in")
    if not checkback_in:
        return f"Tool 'worker/checkback' ({worker_id}): Error: checkback_in (minutes) is required"
    if not worker_id:
        return "Tool 'worker/checkback': Error: worker-id is required"
    try:
        add_checkback(worker_id, carbon_id, float(checkback_in))
        return f"Tool 'worker/checkback' ({worker_id}): Checkback set for {checkback_in} minutes from now"
    except Exception as e:
        return f"Tool 'worker/checkback' ({worker_id}): Error: {e}"


@register(prefix="worker")
def _tool_worker(tool_spec, carbon_id):
    """Dispatch any worker/* tool to the matching worker action."""
    worker_type, action_type, worker_id = _parse_worker_tool(tool_spec)

    if action_type == "new":
        return _worker_new(tool_spec, carbon_id, worker_type, worker_id)
    if action_type == "message":
        return _worker_message(tool_spec, carbon_id, worker_id)
    if action_type == "checkback":
        return _worker_checkback(tool_spec, carbon_id, worker_id)
    if action_type == "status":
        return f"Tool 'worker/status' ({worker_id}): {worker_module.get_worker_status(worker_id, carbon_id)}"
    if action_type == "stop":
        return f"Tool 'worker/stop' ({worker_id}): {stop_worker(worker_id, carbon_id)}"
    if action_type == "list_active":
        return f"Tool 'worker/list_active': {list_active(carbon_id)}"
    if action_type == "list_archive":
        return f"Tool 'worker/list_archive': {list_archive(carbon_id)}"
    if action_type == "read_archive":
        return f"Tool 'worker/read_archive' ({worker_id}): {read_archive(worker_id, carbon_id)}"
    return f"Tool 'worker': Unknown type '{action_type}'"
