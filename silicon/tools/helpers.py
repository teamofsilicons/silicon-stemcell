"""The small readings a tool and the registry both need.

Parsing a worker tool name, naming the progress group a contact is watching,
and exposing an accepted durable identity without its contents. Kept apart from
the registry so a tool can use them without importing the thing that imports
every tool.
"""
from interface.work import current_manager_activity_group

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
