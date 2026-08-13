"""What a manager turn tells the diagnosis store and the contact watching it.

A turn opens a trace, streams provider progress, and closes it. Two rules run
through all of it: an internal root produces no visible activity, and a
provider failure nobody asked for is suppressed rather than shown.
"""
from interface import outbound
from interface import work as work_updates
from silicon import activity as activity_module
import re
import time
from silicon import (
    manager_code,
    parse_manager_output,
    provider_failed,
)
from diagnostics import journal as iwantto_journal
from diagnostics.store import Diagnostics
from interface.progress import (
    diagnostic_error_summary,
    progress_is_error,
    redact_diagnostic_text,
)
from interface.work import (
    settle_manager_activity,

)

from silicon.settings import (
    PROVIDER_PROGRESS_STATES,
    _TERMINAL_BRAIN_FAILURE_MARKERS,
)
from diagnostics.logs import runtime_log as log


def _provider_progress_state(progress):
    kind = str((progress or {}).get("kind") or "")
    if kind in PROVIDER_PROGRESS_STATES:
        return kind
    return "executing"


def _provider_progress_note(progress, line):
    progress = progress or {}
    kind = str(progress.get("kind") or "")
    status = str(progress.get("status") or "")
    text = " ".join(str(line or "").split())
    if not text:
        return ""

    # Keep the UI operational. Provider reasoning summaries are not a safe
    # progress surface, but "thinking" as a state is still useful.
    if kind == "thinking" and status == "output":
        return "thinking"
    if kind == "done":
        return f"provider finished: {text}"
    return text


def _make_provider_progress_handler(carbon_id, group, lifecycle=None):
    last_sent_at_by_key = {}
    last_note_by_key = {}

    def on_progress(progress, line):
        note = _provider_progress_note(progress, line)
        if not note:
            return

        progress = progress or {}
        kind = str(progress.get("kind") or "executing")
        status = str(progress.get("status") or "")
        item_id = str(progress.get("item_id") or "")
        if kind == "done":
            return
        work_updates.touch_manager_call_activity(carbon_id)
        if lifecycle is not None:
            lifecycle.observe(_provider_progress_state(progress))
        key = (kind, status, item_id)
        now = time.time()
        min_interval = 3.0 if status == "output" else 0.6

        if last_note_by_key.get(key) == note and now - last_sent_at_by_key.get(key, 0) < 2.0:
            return
        if now - last_sent_at_by_key.get(key, 0) < min_interval:
            return

        last_note_by_key[key] = note
        last_sent_at_by_key[key] = now
        trace = Diagnostics.get_active_run(carbon_id)
        if trace is not None:
            result_summary = ""
            if kind != "thinking" and status in {"completed", "error"}:
                result_summary = redact_diagnostic_text(
                    progress.get("preview") or progress.get("output") or "",
                    limit=500,
                )
            trace.event(
                "provider.progress",
                provider=str(progress.get("provider") or ""),
                kind=kind,
                status=status,
                item_id=item_id,
                model=str(progress.get("model") or ""),
                duration_ms=progress.get("duration_ms"),
                is_error=bool(progress_is_error(progress)),
                error_summary=diagnostic_error_summary(progress),
                tool_name=str(progress.get("tool_name") or "")[:120],
                path=str(progress.get("path") or "")[:500],
                query=redact_diagnostic_text(progress.get("query") or "", limit=500),
                command=redact_diagnostic_text(progress.get("command") or "", limit=500),
                exit_code=progress.get("exit_code"),
                result_summary=result_summary,
                note=redact_diagnostic_text(note, limit=500),
            )
        frame_key = (
            f"provider:{progress.get('provider') or 'manager'}:{item_id}"
            if item_id
            else (
                f"provider:{progress.get('provider') or 'manager'}:{kind}:"
                f"{progress.get('tool_name') or progress.get('path') or progress.get('query') or status}"
            )
        )
        if group:
            outbound.send_progress(
                carbon_id,
                group,
                _provider_progress_state(progress),
                note,
                frame_key=frame_key,
            )

    return on_progress


def _make_mid_stream_handler(
    carbon_id,
    *,
    allow_intermediate_replies=True,
):
    """Nothing is executed mid-stream any more.

    This existed so a reply could be delivered before the turn ended. Talking is
    `iwantto send` now, which runs the moment the session types it — so there is
    nothing left to intercept, and every remaining tool needs its result fed
    back through the ordered executor anyway. The hook stays because providers
    still call it, and something may yet want it.
    """
    def on_tools(tools_list):
        return []

    return on_tools


def _trace_correlation(context):
    """Extract stable Glass identifiers already present in Interface contexts."""
    text = str(context or "")
    message_ids = list(dict.fromkeys(re.findall(r"^event_id:\s*(\S+)", text, re.MULTILINE)))
    room_ids = list(dict.fromkeys(re.findall(r"^room_id:\s*(\S+)", text, re.MULTILINE)))
    return (room_ids[0] if room_ids else ""), message_ids


def _work_lifecycle_is_visible(trace, context):
    """Expose direct-room roots while hiding explicitly internal continuations."""
    room_id, message_ids = _trace_correlation(context)
    raw_trace_room_id = getattr(trace, "room_id", "") if trace else ""
    raw_trigger = getattr(trace, "trigger", "") if trace else ""
    trace_room_id = (
        raw_trace_room_id.strip()
        if isinstance(raw_trace_room_id, str)
        else ""
    )
    trigger = raw_trigger.strip() if isinstance(raw_trigger, str) else ""
    if room_id or trace_room_id or message_ids or trigger == "message":
        return True
    if trace is None:
        return False
    return trigger not in {
        "handoff",
        "maintenance",
        "manager_loop",
        "startup",
        "worker",
    }


def _is_terminal_brain_failure(output):
    """Return whether another manager round cannot recover this failure."""
    tools_data = parse_manager_output(output or "", debug=False)
    if not tools_data:
        return False
    for tool in tools_data.get("tools", []):
        if not isinstance(tool, dict) or tool.get("tool") != "brain_error":
            continue
        message = str(tool.get("message") or "").lower()
        if any(marker in message for marker in _TERMINAL_BRAIN_FAILURE_MARKERS):
            return True
    return False


def _suppress_undirected_brain_failure(result, carbon_id, visible_activity):
    """Keep an internal brain outage out of a Carbon's inbox."""
    output, rate_limit, executed_tools = result
    if visible_activity or not provider_failed(output, rate_limit):
        return result
    log(
        f"[Silicon] brain unavailable on an undirected root for {carbon_id}; "
        "suppressing the failure reply"
    )
    return '{"tools": [{"tool": "do_nothing"}]}', rate_limit, executed_tools


def _instrumented_manager_call(
    carbon_id,
    text,
    trace,
    iteration,
    on_tools,
    on_progress,
    run_records=None,
):
    """Run one manager turn under its own `iwantto` identity.

    The token is issued per turn and lives only as long as the provider
    subprocess, so a command run mid-turn resolves to this manager and stops
    resolving the moment the turn ends. ``run_records`` collects what the turn
    actually did, which is the only evidence the loop gets that a manager acted
    at all — the actions no longer come back as tool JSON.
    """
    from iwantto.actor import MANAGER, issue_run_env, revoke_actor

    token, env = issue_run_env(MANAGER, carbon_id, carbon_id)
    started = time.monotonic()
    failure = ""
    visible_activity = True
    if run_records is not None:
        visible_activity = bool(
            run_records.get(
                ("visible_activity", str(carbon_id), iteration),
                True,
            )
        )
    try:
        if trace is None:
            result = manager_code(
                text, carbon_id, on_tools=on_tools, on_progress=on_progress,
                env=env,
            )
        else:
            with trace.span(f"round[{iteration}]"):
                with trace.span("manager_turn"):
                    result = manager_code(
                        text, carbon_id, on_tools=on_tools,
                        on_progress=on_progress, trace=trace, env=env,
                    )
        return _suppress_undirected_brain_failure(
            result, carbon_id, visible_activity
        )
    except BaseException as exc:
        failure = type(exc).__name__
        raise
    finally:
        summary = iwantto_journal.run_summary(token)
        if run_records is not None:
            run_records[carbon_id] = summary
        iwantto_journal.record_run(
            MANAGER,
            carbon_id,
            carbon_id,
            trigger=str(text or "")[:200],
            seconds=time.monotonic() - started,
            ok=not failure,
            detail=failure,
            iteration=iteration,
            commands=summary.get("commands") or [],
        )
        iwantto_journal.clear_run(token)
        revoke_actor(token)


def _begin_manager_trace(carbon_id, context, traces):
    """Return (and memoise) the diagnostic trace covering this manager turn.

    Reuses an already-active run when one exists, otherwise opens a new one
    and inherits any room/message correlation handed off from a prior run.
    Tracing must never break the turn, so failures degrade to no trace.
    """
    if carbon_id in traces:
        return traces[carbon_id]
    room_id, message_ids = _trace_correlation(context)
    try:
        pending_contexts = Diagnostics.consume_pending_contexts(carbon_id)
        source_run_ids = list(dict.fromkeys(
            str(item.get("source_run_id") or "")
            for item in pending_contexts
            if item.get("source_run_id")
        ))
        inherited_message_ids = [
            str(event_id)
            for item in pending_contexts
            for event_id in (item.get("message_ids") or [])
            if event_id
        ]
        message_ids = list(dict.fromkeys([*message_ids, *inherited_message_ids]))
        if not room_id:
            room_id = next(
                (str(item.get("room_id") or "") for item in pending_contexts if item.get("room_id")),
                "",
            )
        trace = Diagnostics.get_active_run(carbon_id)
        if trace is None:
            trace = Diagnostics.start_run(
                trigger=(
                    "message" if _trace_correlation(context)[1]
                    else "handoff" if pending_contexts
                    else "manager_loop"
                ),
                carbon_id=carbon_id,
                # Only a single unambiguous source can be claimed as the parent.
                parent_run_id=source_run_ids[0] if len(source_run_ids) == 1 else None,
                room_id=room_id,
                message_ids=message_ids,
                meta={
                    "source_run_ids": source_run_ids,
                    "handoff_ids": [
                        str(item.get("handoff_id") or "")
                        for item in pending_contexts
                        if item.get("handoff_id")
                    ],
                } if pending_contexts else None,
            )
            for event_id in message_ids:
                trace.event("message.ingress", event_id=event_id, room_id=room_id)
            for item in pending_contexts:
                trace.event(
                    "handoff.accepted",
                    handoff_id=str(item.get("handoff_id") or ""),
                    source_run_id=str(item.get("source_run_id") or ""),
                    target_type=str(item.get("target_type") or ""),
                    target_id=str(item.get("target_id") or carbon_id),
                )
            Diagnostics.register_active(carbon_id, trace)
        else:
            for event_id in message_ids:
                trace.add_message(event_id, room_id)
        if trace is not None:
            trace.meta["_manager_running"] = True
    except Exception:
        trace = None
    traces[carbon_id] = trace
    return trace


def _close_manager_trace(carbon_id, traces, activity_groups, long_tasks):
    """Finish one contact's turn: settle its lifecycle, progress group, and trace."""
    lifecycle = long_tasks.pop(carbon_id, None)
    if lifecycle is not None:
        lifecycle.finish(
            keep_alive=activity_module._contact_has_active_workers(carbon_id),
        )
    group = activity_groups.pop(carbon_id, "")
    if group:
        outbound.send_progress(
            carbon_id,
            group,
            "done",
            "manager finished",
            frame_key="manager:done",
        )
        settle_manager_activity(carbon_id, group)
    trace = traces.pop(carbon_id, None)
    Diagnostics.unregister_active(carbon_id, trace)
    if trace is not None:
        try:
            trace.meta.pop("_manager_running", None)
            trace.close()
        except Exception:
            pass
