"""Running the managers that have something waiting.

One turn per contact, serialized per contact and independent across them. A
long task for one Carbon must never hold up another's message.
"""
from interface.long_tasks import registry as lt_registry
from interface import long_tasks as long_tasks_module
from interface import outbound
from interface import work_updates
from manager import activity as activity_module
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from manager.loop import handle_commands
from manager.tools.registry import _manager_output_for_log
from manager.tools.registry import _rate_limit_reply_text
from manager.tools.registry import _tool_results_for_log
from manager.tools.registry import execute_all_tools
from manager.tools.registry import is_only_do_nothing
from manager.tracing import _begin_manager_trace
from manager.tracing import _close_manager_trace
from manager.tracing import _instrumented_manager_call
from manager.tracing import _is_terminal_brain_failure
from manager.tracing import _make_mid_stream_handler
from manager.tracing import _make_provider_progress_handler
from manager.tracing import _trace_correlation
from manager.tracing import _work_lifecycle_is_visible
import json
import time
from manager import (
    parse_manager_output,
    is_rate_limit,
    TIMEOUT_MSG,
)
from worker import (
    get_worker_status,
)
from diagnostics.store import Diagnostics
from interface.progress import (
    redact_diagnostic_text,
)
from interface.work_updates import (
    begin_manager_activity,
    set_active_task_timer,

)
from interface.long_tasks import (
    accuracy_review_root_is_current,
    acknowledge_queued_long_task_root,
    begin_long_task_run,
    close_terminal_accuracy_lifecycle,

    extract_queued_long_task_root_metadata,
    queue_long_task_root_if_blocked,
)

from manager.settings import (
    MANAGER_TIMEOUT_FINAL_REPLY,
    MANAGER_TIMEOUT_RETRY_REPLY,
    MAX_MANAGER_TIMEOUT_RETRIES,
)
from diagnostics.logs import runtime_log as log


def _partition_pending_contexts(context_by_carbon):
    """Split inbound contexts into ordinary turns and internal accuracy reviews.

    Commands are roots too: they must cross the same durable update fence as
    ordinary manager turns rather than running during ingestion.  Returns the
    queued-root bookkeeping alongside the work that is actually ready to run.
    """
    queued_root_ids = {}
    queued_root_visibility = {}
    accuracy_review_ids = {}
    cleaned_contexts = {}
    for contact_id, context in dict(context_by_carbon).items():
        (
            queued_root_id,
            clean_context,
            durable_visibility,
        ) = extract_queued_long_task_root_metadata(context)
        if queued_root_id:
            queued_root_ids[str(contact_id)] = queued_root_id
            if durable_visibility is not None:
                queued_root_visibility[str(contact_id)] = durable_visibility
        accuracy_review_id, clean_context = long_tasks_module.extract_accuracy_review_root(
            clean_context
        )
        if accuracy_review_id:
            # A stale review root has been superseded; drop it entirely.
            if not accuracy_review_root_is_current(
                str(contact_id),
                accuracy_review_id,
            ):
                continue
            accuracy_review_ids[str(contact_id)] = accuracy_review_id
        cleaned_contexts[contact_id] = clean_context

    ordinary_contexts = {
        contact_id: context
        for contact_id, context in cleaned_contexts.items()
        if str(contact_id) not in accuracy_review_ids
    }
    pending = handle_commands(ordinary_contexts) if ordinary_contexts else {}
    pending.update(
        {
            contact_id: cleaned_contexts[contact_id]
            for contact_id in accuracy_review_ids
            if contact_id in cleaned_contexts
        }
    )
    return queued_root_ids, queued_root_visibility, accuracy_review_ids, pending


def _pause_work(carbon_id, long_tasks, note, *, pause_reason="infrastructure"):
    """Pause a contact's work, through its lifecycle when it has one.

    Without a lifecycle there is nothing to journal against, so the durable
    task timer is paused directly instead.
    """
    lifecycle = long_tasks.get(carbon_id)
    if lifecycle is not None:
        lifecycle.defer(note, pause_reason=pause_reason)
    else:
        set_active_task_timer(
            carbon_id,
            timer_state="paused",
            pause_reason=pause_reason,
        )


def run_all_managers(context_by_carbon):
    """Run managers and retain one complete graph for each inbound message batch."""
    (
        queued_root_ids,
        queued_root_visibility,
        accuracy_review_ids,
        pending,
    ) = _partition_pending_contexts(context_by_carbon)
    if not pending:
        return
    max_iterations = 10
    traces = {}
    activity_groups = {}
    long_tasks = {}
    timeout_retries = {}
    accuracy_review_satisfied = set()
    # Deferral is an ordering decision, not a failed accuracy review. A root
    # parked behind the durable long-task queue never reaches the manager and
    # therefore cannot mark itself satisfied during this pass.
    accuracy_review_deferred = set()
    invisible_manager_contacts = set()

    def close_trace(carbon_id):
        _close_manager_trace(carbon_id, traces, activity_groups, long_tasks)

    try:
        for iteration in range(max_iterations):
            if not pending:
                break

            manager_outputs = {}
            already_executed = {}
            iwantto_runs = {}
            with ThreadPoolExecutor(max_workers=max(len(pending), 1)) as executor:
                futures = {}
                for carbon_id, text in pending.items():
                    # A deferred root has not started a manager round.  Do not
                    # create and later complete a diagnostic run until the
                    # durable long-task fence actually admits its provider.
                    try:
                        trace = Diagnostics.get_active_run(carbon_id)
                    except Exception:
                        trace = None
                    group = activity_groups.get(carbon_id)
                    durable_visibility = queued_root_visibility.get(
                        str(carbon_id)
                    )
                    visible_activity = (
                        carbon_id not in accuracy_review_ids
                        and (
                            durable_visibility
                            if durable_visibility is not None
                            else _work_lifecycle_is_visible(trace, text)
                        )
                    )
                    if not visible_activity:
                        invisible_manager_contacts.add(str(carbon_id))
                    _, root_message_ids = _trace_correlation(text)
                    root_run_id = (
                        root_message_ids[0]
                        if root_message_ids
                        else (
                            getattr(trace, "run_id", "")
                            if trace is not None
                            else ""
                        )
                    )
                    if queue_long_task_root_if_blocked(
                        carbon_id,
                        root_run_id,
                        text,
                        visible=visible_activity,
                        claimed_root_id=queued_root_ids.get(
                            str(carbon_id), ""
                        ),
                    ):
                        if carbon_id in accuracy_review_ids:
                            accuracy_review_deferred.add(carbon_id)
                        continue
                    trace = _begin_manager_trace(carbon_id, text, traces)
                    if not futures:
                        log(
                            f"[Silicon] Manager round {iteration + 1} for "
                            f"{list(pending.keys())}..."
                        )
                    if not group and visible_activity:
                        group = begin_manager_activity(
                            carbon_id,
                            getattr(trace, "run_id", "") if trace is not None else "",
                        )
                        activity_groups[carbon_id] = group
                    group = group or ""
                    lifecycle = long_tasks.get(carbon_id)
                    if lifecycle is None:
                        def activity_heartbeat(note, contact_id=carbon_id):
                            activity_group = activity_groups.get(contact_id, "")
                            if activity_group:
                                outbound.send_progress(
                                    contact_id,
                                    activity_group,
                                    "thinking",
                                    note,
                                    frame_key="manager:heartbeat",
                                    occurred_at=time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(),
                                    ),
                                )

                        if carbon_id in accuracy_review_ids:
                            lifecycle = lt_registry.current_long_task(carbon_id)
                        else:
                            lifecycle = begin_long_task_run(
                                carbon_id,
                                root_run_id,
                                text,
                                visible=visible_activity,
                                activity_heartbeat=activity_heartbeat,
                                reply_sender=outbound.reply_contact,
                                has_active_workers=activity_module._contact_has_active_workers,
                                worker_status_resolver=get_worker_status,
                            )
                        if lifecycle is not None:
                            long_tasks[carbon_id] = lifecycle
                            acknowledge_queued_long_task_root(
                                queued_root_ids.pop(str(carbon_id), "")
                            )
                    if (
                        lifecycle is not None
                        and iteration
                        and carbon_id not in accuracy_review_ids
                    ):
                        lifecycle.continuing_round()
                    if (
                        lifecycle is not None
                        and carbon_id not in accuracy_review_ids
                    ):
                        lifecycle.request_running()
                    elif lifecycle is None and carbon_id not in accuracy_review_ids:
                        set_active_task_timer(
                            carbon_id,
                            timer_state="running",
                        )
                    work_updates.touch_manager_call_activity(carbon_id)
                    on_tools = _make_mid_stream_handler(
                        carbon_id,
                        allow_intermediate_replies=(
                            carbon_id not in accuracy_review_ids
                        ),
                    )
                    on_progress = _make_provider_progress_handler(
                        carbon_id,
                        group,
                        (
                            None
                            if carbon_id in accuracy_review_ids
                            else lifecycle
                        ),
                    )
                    if group:
                        outbound.send_progress(
                            carbon_id,
                            group,
                            "thinking",
                            "calling manager",
                            frame_key=f"manager:round:{iteration}",
                        )
                    iwantto_runs[
                        ("visible_activity", str(carbon_id), iteration)
                    ] = visible_activity
                    future = executor.submit(
                        _instrumented_manager_call, carbon_id, text, trace,
                        iteration, on_tools, on_progress, iwantto_runs,
                    )
                    futures[future] = carbon_id

                for future in as_completed(futures):
                    carbon_id = futures[future]
                    try:
                        output, _, executed_tools = future.result()
                        manager_outputs[carbon_id] = output
                        if executed_tools:
                            already_executed[carbon_id] = executed_tools
                    except Exception as exc:
                        if carbon_id in accuracy_review_ids:
                            raise RuntimeError(
                                "internal task accuracy review failed"
                            ) from exc
                        _pause_work(
                            carbon_id,
                            long_tasks,
                            "Work is paused because the manager is unavailable",
                        )
                        safe_error = (
                            redact_diagnostic_text(exc, limit=500)
                            or "manager call failed"
                        )
                        manager_outputs[carbon_id] = json.dumps({
                            "tools": [
                                {
                                    "tool": "reply",
                                    "message": f"Manager error: {safe_error}",
                                },
                                {"tool": "do_nothing"},
                            ]
                        })

            all_tools = []
            pending = {}
            for carbon_id, output in manager_outputs.items():
                tools_data = parse_manager_output(output, debug=False)
                log(
                    f"[Silicon] Manager output for {carbon_id}: "
                    f"{_manager_output_for_log(output, tools_data)}"
                )
                if tools_data is None:
                    if output and is_rate_limit(output):
                        if carbon_id in accuracy_review_ids:
                            raise RuntimeError(
                                "internal task accuracy review was rate-limited"
                            )
                        _pause_work(
                            carbon_id,
                            long_tasks,
                            "Work is paused while the provider is rate-limited",
                            pause_reason="rate_limited",
                        )
                        outbound.reply_contact(_rate_limit_reply_text(output), carbon_id)
                        continue
                    if output == TIMEOUT_MSG:
                        retries = timeout_retries.get(carbon_id, 0)
                        if retries < MAX_MANAGER_TIMEOUT_RETRIES:
                            timeout_retries[carbon_id] = retries + 1
                            outbound.reply_contact(
                                MANAGER_TIMEOUT_RETRY_REPLY,
                                carbon_id,
                                work_continues=True,
                            )
                            pending[carbon_id] = TIMEOUT_MSG
                            continue
                        _pause_work(
                            carbon_id,
                            long_tasks,
                            "Work paused after the manager provider "
                            "stopped responding twice",
                        )
                        outbound.reply_contact(
                            MANAGER_TIMEOUT_FINAL_REPLY,
                            carbon_id,
                        )
                        continue
                    # Managers act through `iwantto` while the turn is running,
                    # so a turn with no tool JSON is the normal case now. What
                    # decides completion is whether anything was actually run.
                    run = iwantto_runs.get(carbon_id) or {}
                    if run.get("did_nothing") or run.get("acted"):
                        if carbon_id in accuracy_review_ids:
                            accuracy_review_satisfied.add(carbon_id)
                        continue
                    pending[carbon_id] = (
                        "You ended without running a single iwantto command."
                        "Running at least one is required."
                        'run `iwantto do-nothing --reason "..."` to say why there is genuinely nothing to do.'
                    )
                    continue

                if is_only_do_nothing(tools_data):
                    if carbon_id in accuracy_review_ids:
                        accuracy_review_satisfied.add(carbon_id)
                    continue
                if (
                    carbon_id in accuracy_review_ids
                    and any(
                        tool_spec.get("tool") == "do_nothing"
                        for tool_spec in tools_data["tools"]
                    )
                ):
                    accuracy_review_satisfied.add(carbon_id)
                executed_keys = {
                    json.dumps(t, sort_keys=True)
                    for t in already_executed.get(carbon_id, [])
                }
                for tool_spec in tools_data["tools"]:
                    key = json.dumps(tool_spec, sort_keys=True)
                    if (
                        carbon_id in accuracy_review_ids
                        and tool_spec.get("tool")
                        not in {"work_update", "do_nothing"}
                    ):
                        continue
                    if tool_spec.get("tool") != "do_nothing" and key not in executed_keys:
                        all_tools.append((carbon_id, tool_spec))

            if all_tools:
                results_by_carbon, remaps = execute_all_tools(
                    all_tools,
                    suppress_progress_contacts={
                        *accuracy_review_ids,
                        *invisible_manager_contacts,
                    },
                )
                log(
                    "[Silicon] Tool results: "
                    f"{_tool_results_for_log(all_tools, results_by_carbon)}"
                )
                for old_id, new_id in remaps.items():
                    if old_id in pending:
                        pending[new_id] = pending.pop(old_id)
                    if old_id in traces:
                        traces[new_id] = traces.pop(old_id)
                        Diagnostics.rename_active(old_id, new_id)
                    if old_id in long_tasks:
                        long_tasks[new_id] = long_tasks.pop(old_id)
                    if old_id in queued_root_visibility:
                        queued_root_visibility[new_id] = (
                            queued_root_visibility.pop(old_id)
                        )
                for carbon_id, results in results_by_carbon.items():
                    if carbon_id in accuracy_review_ids and any(
                        str(result).startswith(
                            "Tool 'work_update': Done."
                        )
                        and "Error:" not in str(result)
                        for result in results
                    ):
                        accuracy_review_satisfied.add(carbon_id)
                    if carbon_id in accuracy_review_ids:
                        close_terminal_accuracy_lifecycle(carbon_id)
                    if results and not _is_terminal_brain_failure(
                        manager_outputs.get(carbon_id, "")
                    ):
                        pending[carbon_id] = "Tool execution results:\n" + "\n".join(results)

            for carbon_id in [cid for cid in traces if cid not in pending]:
                close_trace(carbon_id)

        if pending:
            log(f"[Silicon] Max manager iterations reached. Remaining: {list(pending.keys())}")
            for carbon_id in pending:
                if carbon_id in accuracy_review_ids:
                    continue
                _pause_work(
                    carbon_id,
                    long_tasks,
                    "Work paused after the manager retry budget was exhausted",
                )
                trace = traces.get(carbon_id)
                if trace is None:
                    continue
                try:
                    with trace.span("manager.iteration_limit") as span:
                        span.status = "error"
                        span.set_meta(
                            error="manager retry budget exhausted",
                            max_iterations=max_iterations,
                        )
                    trace.event(
                        "manager.iteration_limit",
                        max_iterations=max_iterations,
                        pending_reason="no_valid_terminal_tool_output",
                    )
                except Exception:
                    pass
            if any(
                carbon_id in accuracy_review_ids
                for carbon_id in pending
            ):
                raise RuntimeError(
                    "internal task accuracy review exhausted its retry budget"
                )
        unsatisfied_accuracy_reviews = (
            set(accuracy_review_ids)
            - accuracy_review_satisfied
            - accuracy_review_deferred
        )
        if unsatisfied_accuracy_reviews:
            raise RuntimeError(
                "internal task accuracy review returned no accepted action"
            )
    finally:
        for carbon_id in list(traces):
            close_trace(carbon_id)
