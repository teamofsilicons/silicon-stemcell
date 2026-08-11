"""The small readings a tool and the registry both need.

Parsing a worker tool name, naming the progress group a contact is watching,
and turning a failed send into the status a work card should show. Kept apart
from the registry so a tool can use them without importing the thing that
imports every tool.
"""
from interface import outbound
from interface.work_updates import current_manager_activity_group

from diagnostics.store import Diagnostics


def _parse_worker_tool(tool_spec):
    """Parse a worker tool spec. Returns (worker_type_or_none, action_type, worker_id)."""
    tool_name = tool_spec.get("tool", "")
    action_type = tool_spec.get("type", "")
    worker_id = tool_spec.get("worker-id", "")

    worker_type = None
    if "/" in tool_name:
        parts = tool_name.split("/", 1)
        if parts[0] == "worker" and parts[1]:
            worker_type = parts[1]

    return worker_type, action_type, worker_id


def _message_failure_status(carbon_id, target_kind, target_id, error):
    message = f"Message failed: {target_kind} '{target_id}' could not be reached. {error}"
    group = _manager_progress_group(carbon_id)
    if group:
        outbound.send_progress(
            carbon_id,
            group,
            "calling",
            message,
        )
    return message


def _call_preparation_failure_status(
    carbon_id,
    target_kind,
    target_id,
    error,
):
    """Report a local call-card barrier failure without exposing its details."""
    error_type = type(error).__name__[:80] or "Exception"
    message = (
        f"Message not sent: the call update for {target_kind} '{target_id}' "
        f"could not be prepared ({error_type})."
    )
    group = _manager_progress_group(carbon_id)
    if group:
        outbound.send_progress(
            carbon_id,
            group,
            "calling",
            message,
        )
    return message


def _work_reference_suffix(reference, *keys):
    """Expose accepted durable identities to the manager without card content."""
    if not isinstance(reference, dict):
        return ""
    values = [
        f"{key}={reference[key]}"
        for key in keys
        if reference.get(key)
    ]
    return f" Work update: {', '.join(values)}." if values else ""


def _manager_progress_group(carbon_id):
    """Return the visible group, suppressing private manager continuations."""
    group = current_manager_activity_group(carbon_id)
    if group:
        return group
    trace = Diagnostics.get_active_run(carbon_id)
    if trace is not None and trace.meta.get("_manager_running"):
        return ""
    # Preserve progress for explicit tool execution outside run_all_managers.
    return f"manager:{carbon_id}"


def _tool_progress_state(tool_spec):
    tool_name = str(tool_spec.get("tool", "") or "")
    action_type = str(tool_spec.get("type", "") or "")
    if tool_name == "message_manager":
        return "calling"
    if tool_name.startswith("worker") and action_type == "new":
        return "spawning_worker"
    return "executing"


def _is_private_manager_tool_name(tool_name):
    return (
        tool_name == "advertising_memory/update"
        or tool_name in {"trust/list", "trust/get"}
        or tool_name == "trust/set"
        or tool_name == "work_update"
    )
