"""Running the tools a manager asked for, in the order that keeps them safe.

A manager answers with a list of tool invocations. They are executed here, with
one ordering rule that is load-bearing: a final reply is the end-of-batch fence,
so durable updates and worker starts are accepted first even when the manager
listed the reply early. Intermediate replies keep their original position.
"""
from interface import outbound
from manager.tools.helpers import (
    _is_private_manager_tool_name,
    _manager_progress_group,
    _parse_worker_tool,
    _tool_progress_state,
)
from manager.tools import handlers  # noqa: F401  (registers every tool)
from manager.tools.base import resolve
from manager.runtime.restart import _do_restart
from diagnostics.store import Diagnostics
from interface.progress import (
    contains_advertising_memory_reference,
    contains_private_manager_tool,
    redact_diagnostic_text,
)


def _tool_progress_note(tool_spec):
    tool_name = str(tool_spec.get("tool", "") or "")
    if not tool_name or tool_name == "do_nothing":
        return ""

    if tool_name == "reply":
        return "called tool: reply"

    if tool_name == "message_manager":
        target = tool_spec.get("carbon_id") or tool_spec.get("silicon_id") or "unknown"
        return f"called tool: message_manager -> {target}"

    if tool_name == "remote_browser":
        action = tool_spec.get("type", "share")
        return f"called tool: remote_browser/{action}"

    if tool_name == "take_back":
        target = tool_spec.get("request_id") or tool_spec.get("event_id") or "unknown"
        return f"called tool: take_back -> {target}"

    if tool_name == "advertising_memory/update":
        return "updating team-visible advertising memory"

    if tool_name in {"trust/list", "trust/get"}:
        return "refreshing the Glass trust policy"

    if tool_name == "trust/set":
        target = tool_spec.get("carbon_id") or tool_spec.get("silicon_id") or "unknown"
        return f"updating trust for {target}"

    if tool_name.startswith("cron/") or tool_name == "cron/list":
        return f"called tool: {tool_name}"

    if tool_name.startswith("worker"):
        worker_type, action_type, worker_id = _parse_worker_tool(tool_spec)
        worker_label = f" {worker_id}" if worker_id else ""
        if action_type == "new":
            kind = worker_type or "worker"
            return f"spawning {kind} worker{worker_label}"
        if action_type:
            return f"called tool: worker/{action_type}{worker_label}"
        return f"called tool: {tool_name}{worker_label}"

    if tool_name == "new_session":
        return "called tool: new_session"

    if tool_name == "restart_silicon_service":
        return "called tool: restart_silicon_service"

    return f"called tool: {tool_name}"


def _diagnostic_tool_metadata(tool_spec):
    """Return only fields that are safe to persist outside manager output."""
    tool_name = str(tool_spec.get("tool", "") or "")
    metadata = {"tool": tool_name}
    if _is_private_manager_tool_name(tool_name):
        return metadata
    target_kind = (
        "silicon" if tool_spec.get("silicon_id")
        else "carbon" if tool_spec.get("carbon_id")
        else ""
    )
    metadata.update(
        action=str(tool_spec.get("type") or ""),
        worker_id=str(tool_spec.get("worker-id") or ""),
        target_kind=target_kind,
        target_id=str(
            tool_spec.get("silicon_id")
            or tool_spec.get("carbon_id")
            or ""
        )[:120],
    )
    return metadata


def execute_single_tool(
    tool_spec,
    carbon_id,
    *,
    suppress_progress=False,
):
    """Execute a single tool, logging the call + result to the daily activity log.

    `do_nothing` is the idle no-op fired on most ticks; logging it would bury the
    real actions, so it's the one thing we skip.
    """
    tool_name = str(tool_spec.get("tool", "") or "")
    trace = Diagnostics.get_active_run(carbon_id)
    if trace is not None and tool_name != "do_nothing":
        result = None
        executed = False
        try:
            with trace.span("tool_call") as span:
                span.set_meta(**_diagnostic_tool_metadata(tool_spec))
                result = _execute_single_tool(
                    tool_spec,
                    carbon_id,
                    suppress_progress=suppress_progress,
                )
                executed = True
                result_status = "error" if "Error" in str(result) else "ok"
                result_summary = (
                    "[Advertising memory result omitted]"
                    if tool_name == "advertising_memory/update"
                    else "[Work update result omitted]"
                    if tool_name == "work_update"
                    else redact_diagnostic_text(result, limit=500)
                )
                span.set_meta(
                    result_status=result_status,
                    result_summary=result_summary,
                )
                if result_status == "error":
                    span.status = "error"
                    span.set_meta(error=result_summary)
        except Exception:
            if not executed:
                result = _execute_single_tool(
                    tool_spec,
                    carbon_id,
                    suppress_progress=suppress_progress,
                )
    else:
        result = _execute_single_tool(
            tool_spec,
            carbon_id,
            suppress_progress=suppress_progress,
        )
    if tool_name and tool_name != "do_nothing":
        try:
            from diagnostics.activity import tool_call
            tool_call(carbon_id, tool_name, tool_spec, result)
        except Exception:
            pass
    return result


def _execute_single_tool(
    tool_spec,
    carbon_id,
    *,
    suppress_progress=False,
):
    """Execute a single tool. Returns result string or None for do_nothing."""
    tool_name = tool_spec.get("tool", "")

    if tool_name == "do_nothing":
        return None

    progress_note = _tool_progress_note(tool_spec)
    if progress_note and not suppress_progress:
        group = _manager_progress_group(carbon_id)
        if group:
            outbound.send_progress(
                carbon_id,
                group,
                _tool_progress_state(tool_spec),
                progress_note,
            )

    tool = resolve(tool_name)
    if tool is None:
        return f"Unknown tool: '{tool_name}'"
    return tool.run(tool_spec, carbon_id)


def execute_all_tools(
    all_tools,
    *,
    suppress_progress_contacts=None,
):
    """Execute all tools from all managers through a single executor.
    all_tools is a list of (carbon_id, tool_spec) tuples.
    Returns (results_by_carbon, empty_remaps_for_legacy_callers)."""
    results_by_carbon = {}
    needs_restart = False
    restart_carbon_id = None
    suppress_progress_contacts = {
        str(contact_id)
        for contact_id in (suppress_progress_contacts or set())
    }

    # A final normal reply is the end-of-batch fence: durable updates and
    # worker starts must be accepted first even if the manager listed the reply
    # early. Intermediate replies intentionally retain their original order.
    restart_tools = [
        (cid, tool)
        for cid, tool in all_tools
        if tool.get("tool") == "restart_silicon_service"
    ]
    final_reply_tools = [
        (cid, tool)
        for cid, tool in all_tools
        if tool.get("tool") == "reply"
        and not tool.get("work_continues")
    ]
    other_tools = [
        (cid, tool)
        for cid, tool in all_tools
        if tool.get("tool") != "restart_silicon_service"
        and not (
            tool.get("tool") == "reply"
            and not tool.get("work_continues")
        )
    ]
    sorted_tools = other_tools + final_reply_tools + restart_tools

    for carbon_id, tool_spec in sorted_tools:
        tool_name = tool_spec.get("tool", "")

        if tool_name == "restart_silicon_service":
            needs_restart = True
            restart_carbon_id = carbon_id
            continue

        if str(carbon_id) in suppress_progress_contacts:
            result = execute_single_tool(
                tool_spec,
                carbon_id,
                suppress_progress=True,
            )
        else:
            result = execute_single_tool(tool_spec, carbon_id)

        if result is not None:
            if carbon_id not in results_by_carbon:
                results_by_carbon[carbon_id] = []
            results_by_carbon[carbon_id].append(result)

    if needs_restart:
        err = _do_restart(restart_carbon_id)
        # Only reaches here if execv failed
        if err:
            if restart_carbon_id not in results_by_carbon:
                results_by_carbon[restart_carbon_id] = []
            results_by_carbon[restart_carbon_id].append(f"Tool 'restart_silicon_service': {err}")

    return results_by_carbon, {}


def _tool_results_for_log(all_tools, results_by_carbon):
    """Keep provider payloads out of the Stemcell's process logs."""
    private_result_contacts = {
        carbon_id
        for carbon_id, tool_spec in all_tools
        if str(tool_spec.get("tool") or "") == "advertising_memory/update"
        or str(tool_spec.get("tool") or "") in {"trust/list", "trust/get"}
        or str(tool_spec.get("tool") or "") == "trust/set"
        or str(tool_spec.get("tool") or "") == "work_update"
    }
    return {
        carbon_id: (
            ["[Private tool result omitted]"]
            if carbon_id in private_result_contacts
            else results
        )
        for carbon_id, results in results_by_carbon.items()
    }


def _manager_output_for_log(output, tools_data):
    """Redact private invocation material before printing manager output."""
    tools = (tools_data.get("tools") or []) if isinstance(tools_data, dict) else []
    parsed_private = any(
        str(tool_spec.get("tool") or "") == "advertising_memory/update"
        or str(tool_spec.get("tool") or "") in {"trust/list", "trust/get"}
        or str(tool_spec.get("tool") or "") == "trust/set"
        or str(tool_spec.get("tool") or "") == "work_update"
        for tool_spec in tools
        if isinstance(tool_spec, dict)
    )
    raw = str(output or "")
    raw_private = contains_private_manager_tool(raw)
    if parsed_private or raw_private:
        return "[Private tool invocation omitted]"
    redacted = redact_diagnostic_text(raw, limit=200)
    if redacted == "[advertising memory content omitted]":
        return "[Advertising memory content omitted]"
    return redacted


def _rate_limit_reply_text(output):
    """Never send a malformed private invocation back to the Carbon."""
    if (
        contains_private_manager_tool(output)
        or contains_advertising_memory_reference(output)
    ):
        return "The manager provider is rate-limited. Please try again shortly."
    return (
        redact_diagnostic_text(output, limit=500)
        or "The manager provider is rate-limited. Please try again shortly."
    )


def is_only_do_nothing(tools_data):
    """Check if the manager returned only do_nothing."""
    if not tools_data or "tools" not in tools_data:
        return True
    tools = tools_data["tools"]
    return len(tools) == 1 and tools[0].get("tool") == "do_nothing"
