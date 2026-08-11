"""Silicon Stemcell process entrypoint and manager event loop.

Responsibilities, in the order they appear below:

* boot the runtime (paths, diagnostics hooks, team context, trust policy)
* run the manager for each contact with pending input, up to a retry budget
* dispatch the tools a manager emits (see ``_TOOL_HANDLERS``)
* drive the periodic handlers listed in ``config.EVENT_LOOP``

Import order matters at the top of this file: the active release must be on
``sys.path`` and ``PATH`` must prefer the selected generation's bin directory
before anything else is imported, so a stale launcher cannot shadow it.
"""
import atexit
import hashlib
import time
import sys
import os
import json
import re
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure the active immutable release is importable before resolving the
# durable instance root used by side-by-side updates.
CODE_ROOT = os.path.dirname(os.path.abspath(__file__))
if CODE_ROOT not in sys.path:
    sys.path.insert(0, CODE_ROOT)
from helpers.paths import DATA_ROOT

PROJECT_ROOT = os.fspath(DATA_ROOT)
LOCAL_BIN = os.path.join(PROJECT_ROOT, ".local", "bin")
ACTIVE_ENV_BIN = os.path.dirname(os.path.abspath(sys.executable))
_path_entries = [
    entry
    for entry in os.environ.get("PATH", "").split(os.pathsep)
    if entry and entry not in {ACTIVE_ENV_BIN, LOCAL_BIN}
]
# Package entry points belong to the selected generation environment. Keep that
# bin directory ahead of the legacy data-root bin so an old generated launcher
# can never shadow an installed command.
os.environ["PATH"] = os.pathsep.join(
    [ACTIVE_ENV_BIN, LOCAL_BIN, *_path_entries]
)

from config import EVENT_LOOP, LOOP_TICK, acknowledge_team_context_result
from manager import (
    INJECTED_PREFIX,
    manager_code,
    parse_manager_output,
    new_session,
    is_rate_limit,
    provider_failed,
    TIMEOUT_MSG,
)
from interface.adapter import (
    complete_take_back,
    ensure_contact_for_target,
    get_contact,
    get_contacts,
    maintenance_inbox_quiescent,
    remote_browser_close,
    remote_browser_share,
    reply_contact as reply_user,
    schedule_maintenance_notices,
    send_progress,
    start_listener,
    start_runtime_file_watch,
    stop_listener,
    stop_runtime_file_watch,
    take_back_event,
    validate_contacts_integrity,
    runtime_file_notifications_active,
    wait_for_runtime_activity,
)
from interface.messages import MANAGER_MESSAGES_FILE, send_manager_message
from diagnostics.iwantto import injection
from diagnostics.iwantto import journal as iwantto_journal
from interface.cron import CRON_INVALIDATION_FILE, execute_cron_tool
from manager.runtime.maintenance import (
    COORDINATOR as MAINTENANCE,
    RootAdmission,
    heartbeat_scope,
)
from manager.runtime.health import start_runtime_health, stop_runtime_health
from worker.handler import (
    ACTIVE_FILE,
    BROWSER_QUEUE_FILE,
    start_worker,
    message_worker,
    get_worker_status,
    stop_worker,
    list_active,
    list_archive,
    read_archive,
    reconcile_maintenance_activities,
)
from interface.cron.checkback import add_checkback
from diagnostics.store import Diagnostics
from interface.progress import (
    contains_advertising_memory_reference,
    contains_private_manager_tool,
    diagnostic_error_summary,
    progress_is_error,
    redact_diagnostic_text,
)
from interface.work_updates import (
    WORK_UPDATES_FILE,
    begin_manager_activity,
    complete_inactive_calls,
    current_manager_activity_group,
    execute_work_update,
    prepare_outbound_call,
    record_worker_started,
    next_inactive_call_deadline,
    set_active_task_timer,
    settle_manager_activity,
    touch_manager_call_activity,
)
from interface.long_tasks import (
    LONG_TASK_STATE_FILE,
    accuracy_review_root_is_current,
    acknowledge_accuracy_review_dispatched,
    acknowledge_queued_long_task_root,
    begin_long_task_run,
    backfill_active_estimated_task_lifecycles,
    claim_ready_accuracy_review_roots,
    claim_ready_long_task_roots,
    close_terminal_accuracy_lifecycle,
    complete_accuracy_review_root,
    current_long_task,
    extract_accuracy_review_root,
    extract_queued_long_task_root_metadata,
    queue_long_task_root_if_blocked,
    recover_long_task_lifecycles,
)


# One fresh-thread retry bounds a silent provider failure to two inactivity
# windows. A second timeout pauses the durable task and releases the contact
# dispatcher instead of occupying it for the remaining manager iterations.
MAX_MANAGER_TIMEOUT_RETRIES = 1
MANAGER_TIMEOUT_RETRY_REPLY = (
    "The manager stopped responding before it produced a result. "
    "I’m retrying once with a fresh session."
)
MANAGER_TIMEOUT_FINAL_REPLY = (
    "I couldn’t complete this request because the manager provider stopped "
    "responding twice. The task is paused; send a new message to resume it."
)

RESTART_FLAG = os.path.join(PROJECT_ROOT, ".restart_pending")


def _install_diagnostic_shutdown_hooks():
    """Persist in-flight evidence on normal exit and catchable termination."""
    atexit.register(
        Diagnostics.close_active_runs,
        reason="Silicon process exited before the run completed",
        category="process_exit",
    )

    def shutdown(signum, _frame):
        try:
            signal_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            signal_name = str(signum)
        Diagnostics.close_active_runs(
            reason=f"Silicon process received {signal_name}",
            category="process_signal",
        )
        raise SystemExit(128 + int(signum))

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, shutdown)


def log(msg):
    print(msg, flush=True)


def _bootstrap_team_context():
    """Best-effort team mirror before any manager can receive a startup turn."""
    try:
        from interface.team_context import reconcile_team_context

        return reconcile_team_context(
            PROJECT_ROOT,
            force=True,
            reason="startup",
        )
    except Exception as exc:
        log(f"[Silicon] Team context startup sync skipped: {exc}")
        return None


def _bootstrap_trust_policy():
    """Best-effort Glass trust sync before any manager can receive a turn."""
    try:
        from interface.trust import reconcile_trust_policy

        result = reconcile_trust_policy(
            PROJECT_ROOT,
            force=True,
            reason="startup",
        )
        if result.get("status") == "deferred":
            log(
                "[Silicon] Trust policy startup sync deferred; managers will "
                "fail closed at very_low until Glass confirms it."
            )
        return result
    except Exception as exc:
        log(f"[Silicon] Trust policy startup sync skipped: {exc}")
        return None


PROVIDER_PROGRESS_STATES = {
    "reading_file",
    "writing_file",
    "executing",
    "searching_web",
    "thinking",
}


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
        touch_manager_call_activity(carbon_id)
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
            send_progress(
                carbon_id,
                group,
                _provider_progress_state(progress),
                note,
                frame_key=frame_key,
            )

    return on_progress


# --- Restart handling ---

def _check_restart_flag():
    """Check if we just restarted. Returns (message, carbon_id_or_None)."""
    if not os.path.exists(RESTART_FLAG):
        return "", None

    try:
        with open(RESTART_FLAG) as f:
            raw = f.read().strip()
        os.remove(RESTART_FLAG)

        # Try JSON format (new)
        try:
            info = json.loads(raw)
            carbon_id = info.get("carbon_id")
            msg = f"Silicon service restarted successfully. {info.get('message', '')}"
            return msg, carbon_id
        except (json.JSONDecodeError, ValueError):
            # Legacy format - just text
            return f"Silicon service restarted successfully. {raw}", None
    except Exception as e:
        try:
            os.remove(RESTART_FLAG)
        except Exception:
            pass
        return f"Silicon service restarted, but error reading restart info: {e}", None


RESTART_REQUEST_FILE = os.path.join(PROJECT_ROOT, ".restart_requested")


def _check_restart_request():
    """Honour an `iwantto restart-silicon` request left by a manager.

    A command runs in a child process and cannot re-exec the Stemcell, so it
    writes a request here and this loop performs the restart.
    """
    if not os.path.exists(RESTART_REQUEST_FILE):
        return
    carbon_id = None
    try:
        with open(RESTART_REQUEST_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
        carbon_id = payload.get("requested_by") or None
        log(f"[Silicon] Restart requested: {payload.get('reason', '')}")
    except Exception:
        pass
    try:
        os.remove(RESTART_REQUEST_FILE)
    except OSError:
        pass
    error = _do_restart(carbon_id)
    if error:
        log(f"[Silicon] {error}")


def _do_restart(carbon_id=None):
    """Write flag file and re-exec the process."""
    try:
        with open(RESTART_FLAG, "w") as f:
            json.dump({
                "carbon_id": carbon_id,
                "message": f"Restarted at {time.strftime('%Y-%m-%d %H:%M:%S')}",
            }, f)
        log("[Silicon] Restart requested. Re-execing...")
        os.execv(sys.executable, [sys.executable, "-u"] + sys.argv)
    except Exception as e:
        try:
            os.remove(RESTART_FLAG)
        except Exception:
            pass
        return f"Error: restart failed - {e}"


# --- Tool parsing and execution ---

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


def _message_failure_status(carbon_id, target_kind, target_id, error):
    message = f"Message failed: {target_kind} '{target_id}' could not be reached. {error}"
    group = _manager_progress_group(carbon_id)
    if group:
        send_progress(
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
        send_progress(
            carbon_id,
            group,
            "calling",
            message,
        )
    return message


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


# --- Tool handlers ---------------------------------------------------------
#
# One handler per manager tool.  Each takes the raw tool spec plus the carbon
# it runs for, and returns the string echoed back to the manager (or None to
# stay silent).  _TOOL_HANDLERS maps exact tool names onto these; tools whose
# names carry a suffix ("cron/add", "worker/new") are matched by prefix in
# _TOOL_PREFIX_HANDLERS.


def _tool_work_update(tool_spec, carbon_id):
    """Record progress against the carbon's open long-running task."""
    lifecycle = current_long_task(carbon_id)
    prepared = (
        lifecycle.prepare_work_update(tool_spec)
        if lifecycle is not None
        else [tool_spec]
    )
    results = [
        execute_work_update(prepared_spec, carbon_id)
        for prepared_spec in prepared
    ]
    if lifecycle is not None:
        lifecycle.record_work_update(tool_spec, prepared, results)
    return "Tool 'work_update': " + " ".join(str(result) for result in results)


def _tool_reply(tool_spec, carbon_id):
    """Send the manager's reply. Unless work continues, this closes the task."""
    message = tool_spec.get("message", "")
    work_continues = bool(tool_spec.get("work_continues", False))
    lifecycle = current_long_task(carbon_id)
    if lifecycle is not None and not work_continues:
        status = lifecycle.deliver_final_reply(
            message,
            has_active_workers=_contact_has_active_workers(carbon_id),
            reply_sender=reply_user,
        )
    else:
        status = reply_user(
            message,
            carbon_id,
            work_continues=work_continues,
        )
    return f"Tool 'reply': {status}"


def _tool_message_manager(tool_spec, carbon_id):
    """Relay a message to another carbon's or silicon's manager as a work call."""
    message = tool_spec.get("message", "")
    if not message:
        return "Tool 'message_manager': Error: message is required"

    # Carbons are addressed through their manager; silicons by their own name.
    target_carbon_id = tool_spec.get("carbon_id", "")
    target_silicon_id = tool_spec.get("silicon_id", "")
    if target_carbon_id:
        contact_type, requested_id, target_kind = "carbon", target_carbon_id, "manager"
    elif target_silicon_id:
        contact_type, requested_id, target_kind = "silicon", target_silicon_id, "silicon"
    else:
        return "Tool 'message_manager': Error: carbon_id or silicon_id is required"

    lifecycle = current_long_task(carbon_id)
    call_task_id = (
        lifecycle.resolve_task_id(str(tool_spec.get("task_id") or ""))
        if lifecycle is not None
        else str(tool_spec.get("task_id") or "")
    )

    try:
        contact = ensure_contact_for_target(contact_type, requested_id)
    except Exception as e:
        status = _message_failure_status(carbon_id, contact_type, requested_id, e)
        return f"Tool 'message_manager' (to {requested_id}): Error: {status}"

    target_id = contact.get(f"{contact_type}_id") or requested_id
    display = contact.get("display_name") or contact.get("name") or target_id
    target_name = f"{display}'s manager" if contact_type == "carbon" else str(display)

    try:
        work_call = prepare_outbound_call(
            carbon_id,
            target_kind=target_kind,
            target_id=target_id,
            target_name=target_name,
            message=message,
            task_id=call_task_id,
        )
    except Exception as exc:
        status = _call_preparation_failure_status(
            carbon_id,
            contact_type,
            target_id,
            exc,
        )
        return f"Tool 'message_manager' (to {target_id}): Error: {status}"

    status = send_manager_message(
        carbon_id,
        target_id,
        message,
        target_type=contact_type,
        work_call=work_call,
    )
    return (
        f"Tool 'message_manager' (to {target_id}): {status}"
        + _work_reference_suffix(
            work_call,
            "task_id",
            "work_event_id",
            "call_id",
        )
    )


def _tool_trust_inspect(tool_spec, carbon_id):
    """Read the effective trust policy for one contact, or list every contact."""
    tool_name = tool_spec.get("tool", "")
    target_carbon_id = str(tool_spec.get("carbon_id") or "").strip()
    target_silicon_id = str(tool_spec.get("silicon_id") or "").strip()
    if tool_name == "trust/get" and (
        bool(target_carbon_id) == bool(target_silicon_id)
    ):
        return (
            "Tool 'trust/get': Error: provide exactly one of carbon_id "
            "or silicon_id"
        )
    if tool_name == "trust/list" and target_carbon_id and target_silicon_id:
        return (
            "Tool 'trust/list': Error: provide at most one of carbon_id "
            "or silicon_id"
        )
    try:
        from interface.trust import inspect_trust_policy

        policy = inspect_trust_policy(
            kind=(
                "carbon"
                if target_carbon_id
                else "silicon"
                if target_silicon_id
                else ""
            ),
            public_id=target_carbon_id or target_silicon_id,
            root=PROJECT_ROOT,
            refresh=True,
        )
    except Exception as exc:
        return f"Tool '{tool_name}': Error: {exc}"
    return f"Tool '{tool_name}': {json.dumps(policy, sort_keys=True)}"


def _tool_trust_set(tool_spec, carbon_id):
    """Change one contact's trust level, recording who initiated the change."""
    target_carbon_id = str(tool_spec.get("carbon_id") or "").strip()
    target_silicon_id = str(tool_spec.get("silicon_id") or "").strip()
    if bool(target_carbon_id) == bool(target_silicon_id):
        return (
            "Tool 'trust/set': Error: provide exactly one of carbon_id "
            "or silicon_id"
        )
    raw_level = tool_spec.get("level")
    # An empty/inherit level clears the override and falls back to the team default.
    level = None if raw_level in {None, "", "inherit", "team_default"} else str(raw_level)
    try:
        from interface.trust import set_contact_trust

        initiating_contact = get_contact(carbon_id) or {}
        result = set_contact_trust(
            "carbon" if target_carbon_id else "silicon",
            target_carbon_id or target_silicon_id,
            level,
            reason=str(tool_spec.get("reason") or ""),
            initiated_by_carbon_id=(
                carbon_id
                if initiating_contact.get("contact_type") == "carbon"
                else ""
            ),
            root=PROJECT_ROOT,
        )
    except Exception as exc:
        return f"Tool 'trust/set': Error: {exc}"
    return (
        f"Tool 'trust/set': {result['target']} is now "
        f"{result['level']} at Glass revision {result['revision']}"
    )


def _tool_remote_browser(tool_spec, carbon_id):
    """Share or close the carbon-visible remote browser session."""
    action_type = tool_spec.get("type", "share")
    if action_type == "share":
        expiry = tool_spec.get("expiry", 60)
        new = tool_spec.get("new", True)
        start_url = tool_spec.get("url") or tool_spec.get("start_url") or ""
        status = remote_browser_share(carbon_id, expiry=expiry, new=new, url=start_url)
        return f"Tool 'remote_browser/share': {status}"
    if action_type == "close":
        status = remote_browser_close(carbon_id)
        return f"Tool 'remote_browser/close': {status}"
    return f"Tool 'remote_browser': Unknown type '{action_type}'"


def _tool_take_back(tool_spec, carbon_id):
    """Complete a pending take-back request, or retract an already-sent event."""
    request_id = tool_spec.get("request_id", "")
    event_id = tool_spec.get("event_id", "")
    if request_id:
        status = complete_take_back(request_id, tool_spec.get("message", ""))
        return f"Tool 'take_back': {status}"
    if event_id:
        status = take_back_event(
            event_id,
            reason=tool_spec.get("reason", ""),
            force=bool(tool_spec.get("force", False)),
        )
        return f"Tool 'take_back': {status}"
    return "Tool 'take_back': Error: request_id or event_id is required"


def _tool_advertising_memory_update(tool_spec, carbon_id):
    """Publish this silicon's advertising memory to Glass."""
    content = tool_spec.get("content")
    if not isinstance(content, str):
        return "Tool 'advertising_memory/update': Error: content must be a string"
    resolve_conflict = tool_spec.get("resolve_conflict", False)
    if not isinstance(resolve_conflict, bool):
        return (
            "Tool 'advertising_memory/update': Error: "
            "resolve_conflict must be a boolean"
        )
    try:
        from interface.team_context import update_own_advertising_memory

        outcome = update_own_advertising_memory(
            content,
            root=PROJECT_ROOT,
            resolve_conflict=resolve_conflict,
        )
    except Exception as exc:
        return f"Tool 'advertising_memory/update': Error: {exc}"

    if not isinstance(outcome, dict):
        return f"Tool 'advertising_memory/update': {outcome or 'saved'}"

    if outcome.get("ok") is True:
        acknowledge_team_context_result(outcome)
    status = str(outcome.get("status") or "saved")
    details = []
    revision = outcome.get("revision")
    actual_revision = outcome.get("actual_revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        details.append(f"revision {revision}")
    if isinstance(actual_revision, int) and not isinstance(actual_revision, bool):
        details.append(f"Glass is at revision {actual_revision}")
    if outcome.get("local_saved") and outcome.get("ok") is False:
        details.append("local draft preserved")
    detail = str(outcome.get("detail") or "").strip()
    if detail:
        details.append(detail)
    suffix = f" — {'; '.join(details)}" if details else ""
    error_prefix = "Error: " if outcome.get("ok") is False else ""
    return f"Tool 'advertising_memory/update': {error_prefix}{status}{suffix}"


def _tool_cron(tool_spec, carbon_id):
    """Run any cron/* tool; interface.cron owns the per-action behaviour."""
    try:
        return execute_cron_tool(tool_spec)
    except Exception as e:
        return f"Tool '{tool_spec.get('tool', '')}': Error: {e}"


def _worker_new(tool_spec, carbon_id, worker_type, worker_id):
    """Spawn a worker, journalling the intent first so a crash can't lose it."""
    if not worker_type:
        return "Tool 'worker/new': Error: worker_type is required. Use worker/browser, worker/terminal, or worker/writer"
    if not worker_id:
        return "Tool 'worker/new': Error: worker-id is required"
    task = tool_spec.get("task", "")
    if not task:
        return f"Tool 'worker/new' ({worker_id}): Error: task is required"

    lifecycle = current_long_task(carbon_id)
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
        return f"Tool 'worker/status' ({worker_id}): {get_worker_status(worker_id, carbon_id)}"
    if action_type == "stop":
        return f"Tool 'worker/stop' ({worker_id}): {stop_worker(worker_id, carbon_id)}"
    if action_type == "list_active":
        return f"Tool 'worker/list_active': {list_active(carbon_id)}"
    if action_type == "list_archive":
        return f"Tool 'worker/list_archive': {list_archive(carbon_id)}"
    if action_type == "read_archive":
        return f"Tool 'worker/read_archive' ({worker_id}): {read_archive(worker_id, carbon_id)}"
    return f"Tool 'worker': Unknown type '{action_type}'"


def _tool_new_session(tool_spec, carbon_id):
    """Start a fresh manager session, dropping the current conversation."""
    return f"Tool 'new_session': Done. New session id: {new_session(carbon_id)}"


def _tool_restart_silicon_service(tool_spec, carbon_id):
    """No-op here: execute_all_tools performs the restart after the batch."""
    return None


_TOOL_HANDLERS = {
    "work_update": _tool_work_update,
    "reply": _tool_reply,
    "message_manager": _tool_message_manager,
    "trust/list": _tool_trust_inspect,
    "trust/get": _tool_trust_inspect,
    "trust/set": _tool_trust_set,
    "remote_browser": _tool_remote_browser,
    "take_back": _tool_take_back,
    "advertising_memory/update": _tool_advertising_memory_update,
    "new_session": _tool_new_session,
    "restart_silicon_service": _tool_restart_silicon_service,
}

# Checked only after an exact match fails, so a specific tool name always wins.
_TOOL_PREFIX_HANDLERS = (
    ("cron/", _tool_cron),
    ("worker", _tool_worker),
)


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
            send_progress(
                carbon_id,
                group,
                _tool_progress_state(tool_spec),
                progress_note,
            )

    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        for prefix, prefix_handler in _TOOL_PREFIX_HANDLERS:
            if tool_name.startswith(prefix):
                handler = prefix_handler
                break
    if handler is None:
        return f"Unknown tool: '{tool_name}'"
    return handler(tool_spec, carbon_id)


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


# --- Per-carbon command handling ---

def handle_commands(context_by_carbon):
    """Handle /new and /start commands per carbon. Returns cleaned context dict."""
    cleaned = {}
    for carbon_id, context in context_by_carbon.items():
        if "[COMMAND: NEW_SESSION]" in context:
            new_id = new_session(carbon_id)
            reply_user("New session started. Fresh context loaded.", carbon_id)
            log(f"[Silicon] New session for {carbon_id}: {new_id}")
            context = context.replace("[COMMAND: NEW_SESSION]", "").strip()

        if "[COMMAND: START]" in context:
            reply_user("Silicon is online and ready.", carbon_id)
            context = context.replace("[COMMAND: START]", "").strip()

        if context:
            cleaned[carbon_id] = context
        else:
            trace = Diagnostics.get_active_run(carbon_id)
            if trace is not None:
                Diagnostics.unregister_active(carbon_id, trace)
                trace.close()
    return cleaned


# --- Event loop ---

def run_event_loop_tick(handler_names=None):
    """Run selected event handlers. Returns {carbon_id: context_string}."""
    context_by_carbon = {}
    selected = None if handler_names is None else set(handler_names)

    for handler in EVENT_LOOP:
        if selected is not None and handler["name"] not in selected:
            continue
        try:
            result = handler["execute"]()
            if not result:
                continue

            if isinstance(result, dict):
                # Multi-user handler returns {carbon_id: context_string}
                for carbon_id, ctx in result.items():
                    if ctx:
                        if carbon_id not in context_by_carbon:
                            context_by_carbon[carbon_id] = []
                        context_by_carbon[carbon_id].append(ctx)
            elif isinstance(result, str) and result:
                log(f"[Silicon] Warning: handler '{handler['name']}' returned string instead of dict")

        except Exception as e:
            log(f"[Silicon] Error in {handler['name']}: {e}")

    # Merge context lists into strings
    merged = {}
    for carbon_id, parts in context_by_carbon.items():
        merged[carbon_id] = "\n\n".join(parts)

    return merged


class EventLoopSchedule:
    """Independent, deterministic recovery clocks for event-loop handlers."""

    def __init__(self, handlers, *, now=None, identity=""):
        self.handlers = {handler["name"]: handler for handler in handlers}
        self.identity = str(identity or PROJECT_ROOT)
        self.attempts = {name: 0 for name in self.handlers}
        current = time.monotonic() if now is None else float(now)
        self.next_due = {}
        for name, handler in self.handlers.items():
            if handler.get("run_on_startup"):
                self.next_due[name] = current
            else:
                self.next_due[name] = current + self._delay(name, handler)

    def _delay(self, name, handler):
        interval = max(0.1, float(handler.get("interval_seconds") or LOOP_TICK))
        jitter = max(0.0, float(handler.get("jitter_seconds") or 0.0))
        attempt = self.attempts[name]
        digest = hashlib.sha256(
            f"{self.identity}:{name}:{attempt}".encode("utf-8")
        ).digest()
        fraction = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
        return interval + (jitter * fraction)

    def due(self, now, *, activity=False, eligible=None):
        allowed = set(self.handlers) if eligible is None else set(eligible)
        names = {
            name
            for name in allowed
            if name in self.next_due and now >= self.next_due[name]
        }
        if activity:
            names.update(
                name
                for name in allowed
                if self.handlers.get(name, {}).get("run_on_activity")
            )
        return names

    def record_attempts(self, names, now):
        for name in names:
            deadline = self.next_due.get(name)
            # An event-triggered attempt must not postpone the independent
            # recovery clock unless that clock was itself due.
            if deadline is None or now < deadline:
                continue
            self.attempts[name] += 1
            self.next_due[name] = now + self._delay(name, self.handlers[name])

    def seconds_until_due(self, now, *, eligible=None):
        allowed = set(self.handlers) if eligible is None else set(eligible)
        deadlines = [
            deadline
            for name, deadline in self.next_due.items()
            if name in allowed
        ]
        if not deadlines:
            return float(LOOP_TICK)
        return max(0.0, min(deadlines) - now)


def _make_mid_stream_handler(
    carbon_id,
    *,
    allow_intermediate_replies=True,
):
    """Create a callback that executes reply tools mid-stream for fast delivery.
    Only explicitly intermediate replies are fire-and-forget. Final replies
    stay in the ordered executor so a terminal card or other preceding update
    is accepted first. All non-reply tools need their results fed back to the
    manager and therefore also use the centralized executor."""
    def on_tools(tools_list):
        if not allow_intermediate_replies:
            return []
        succeeded = []
        for tool_spec in tools_list:
            tool_name = tool_spec.get("tool", "")
            if tool_name != "reply" or not tool_spec.get("work_continues"):
                continue
            result = execute_single_tool(tool_spec, carbon_id)
            if result:
                log(f"[Silicon] Mid-stream: {result}")
                if "Error" not in result:
                    succeeded.append(tool_spec)
        return succeeded
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


_TERMINAL_BRAIN_FAILURE_MARKERS = ("usage limit", "not authenticated")


def _is_terminal_brain_failure(output):
    """Return whether another manager round cannot recover this failure."""
    tools_data = parse_manager_output(output or "", debug=False)
    if not tools_data:
        return False
    for tool in tools_data.get("tools", []):
        if not isinstance(tool, dict) or tool.get("tool") != "reply":
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
    from diagnostics.iwantto.actor import MANAGER, issue_run_env, revoke_actor

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


def _contact_has_active_workers(carbon_id):
    try:
        return not list_active(carbon_id).startswith(
            "No active or queued workers."
        )
    except Exception:
        # Do not terminalize a task when worker state cannot be inspected
        # reliably.
        return True


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
        accuracy_review_id, clean_context = extract_accuracy_review_root(
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
            keep_alive=_contact_has_active_workers(carbon_id),
        )
    group = activity_groups.pop(carbon_id, "")
    if group:
        send_progress(
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
                                send_progress(
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
                            lifecycle = current_long_task(carbon_id)
                        else:
                            lifecycle = begin_long_task_run(
                                carbon_id,
                                root_run_id,
                                text,
                                visible=visible_activity,
                                activity_heartbeat=activity_heartbeat,
                                reply_sender=reply_user,
                                has_active_workers=_contact_has_active_workers,
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
                    touch_manager_call_activity(carbon_id)
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
                        send_progress(
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
                        reply_user(_rate_limit_reply_text(output), carbon_id)
                        continue
                    if output == TIMEOUT_MSG:
                        retries = timeout_retries.get(carbon_id, 0)
                        if retries < MAX_MANAGER_TIMEOUT_RETRIES:
                            timeout_retries[carbon_id] = retries + 1
                            reply_user(
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
                        reply_user(
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


class ManagerDispatcher:
    """Serialize turns per contact while allowing unrelated contacts to run.

    Interface ingestion stays live while managers and workers are busy. A new
    message for an active contact is coalesced into that contact's next turn;
    a message for another contact starts independently.
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
            accuracy_review_id, _ = extract_accuracy_review_root(str(context))
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
                        review_id, _ = extract_accuracy_review_root(
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
                            # Internal accuracy reviews stay isolated from
                            # user roots; every admission remains leased until
                            # its own manager turn actually returns.
                            with heartbeat_scope(
                                [item.activity for item in batch],
                                coordinator=MAINTENANCE,
                            ):
                                self._runner({
                                    carbon_id: "\n\n".join(
                                        item.context for item in batch
                                    )
                                })
                            for item in batch:
                                review_id, _ = extract_accuracy_review_root(
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


def main():
    _install_diagnostic_shutdown_hooks()
    log("[Silicon] Starting event loop...")
    log(
        "[Silicon] Event-driven scheduler active; independent recovery "
        f"checks are capped at {LOOP_TICK}s for interactive sources"
    )

    _bootstrap_team_context()
    _bootstrap_trust_policy()
    start_listener()
    start_runtime_file_watch(
        [
            MAINTENANCE.state_file,
            MANAGER_MESSAGES_FILE,
            WORK_UPDATES_FILE,
            LONG_TASK_STATE_FILE,
            ACTIVE_FILE,
            BROWSER_QUEUE_FILE,
            CRON_INVALIDATION_FILE,
        ]
    )
    recovered_long_tasks = recover_long_task_lifecycles(
        reply_sender=reply_user,
        has_active_workers=_contact_has_active_workers,
        worker_status_resolver=get_worker_status,
    )
    if recovered_long_tasks:
        log(
            "[Silicon] Replaying "
            f"{recovered_long_tasks} durable long-task lifecycle(s)."
        )
    backfilled_long_tasks = backfill_active_estimated_task_lifecycles(
        reply_sender=reply_user,
        has_active_workers=_contact_has_active_workers,
        worker_status_resolver=get_worker_status,
    )
    if backfilled_long_tasks:
        log(
            "[Silicon] Backfilled "
            f"{backfilled_long_tasks} active estimated task lifecycle(s)."
        )
    dispatcher = ManagerDispatcher()
    start_runtime_health(
        lambda: str(MAINTENANCE.public_status().get("phase", "available"))
    )

    # Check if we just restarted
    restart_msg, restart_carbon_id = _check_restart_flag()
    if restart_msg:
        if restart_carbon_id:
            log(f"[Silicon] Post-restart for {restart_carbon_id}: {restart_msg}")
            dispatcher.submit({restart_carbon_id: restart_msg})
        else:
            # Find central carbon to notify
            log(f"[Silicon] Post-restart (no carbon_id): {restart_msg}")
            contacts_data = get_contacts()
            for cid, info in contacts_data.get("contacts", {}).items():
                if info.get("is_central_carbon"):
                    dispatcher.submit({cid: restart_msg})
                    break

    schedule = EventLoopSchedule(EVENT_LOOP, identity=PROJECT_ROOT)
    activity_ready = True
    next_contact_integrity = 0.0
    contact_jitter = int.from_bytes(
        hashlib.sha256(f"{PROJECT_ROOT}:contacts".encode("utf-8")).digest()[:2],
        "big",
    ) % 61
    failure_count = 0
    failure_not_before = 0.0
    try:
        while True:
            now = time.monotonic()
            if now < failure_not_before:
                # A global scheduler failure should never turn into a hot
                # retry loop, even if a burst of pending wakeups is queued.
                time.sleep(failure_not_before - now)
                activity_ready = True
            maintenance_active = False
            eligible_handlers = set(schedule.handlers)
            try:
                maintenance_status = _maintenance_runtime_tick(
                    dispatcher,
                    attest=False,
                )
                maintenance_active = (
                    maintenance_status.get("phase") != "available"
                )
                now = time.monotonic()
                if not maintenance_active:
                    _check_restart_request()
                if not maintenance_active and now >= next_contact_integrity:
                    validate_contacts_integrity()
                    next_contact_integrity = now + 300.0 + contact_jitter

                if maintenance_active:
                    eligible_handlers = {
                        "check_interface",
                        "check_manager_messages",
                        "check_workers",
                    }
                else:
                    eligible_handlers = set(schedule.handlers)
                selected_handlers = schedule.due(
                    now,
                    activity=activity_ready,
                    eligible=eligible_handlers,
                )
                activity_ready = False
                context_by_carbon = _merge_due_internal_roots(
                    run_event_loop_tick(selected_handlers),
                    maintenance_active=maintenance_active,
                )
                schedule.record_attempts(selected_handlers, now)
                call_deadline = next_inactive_call_deadline()
                if (
                    call_deadline is not None
                    and call_deadline <= time.time()
                ):
                    try:
                        complete_inactive_calls()
                    except Exception as exc:
                        log(
                            "[Work updates] inactive call completion "
                            f"deferred: {exc}"
                        )
                if context_by_carbon:
                    for cid, ctx in context_by_carbon.items():
                        log(f"[Silicon] Context for {cid}:\n{ctx[:200]}...")
                    dispatcher.submit(context_by_carbon)
                _maintenance_runtime_tick(dispatcher, attest=True)
                failure_count = 0
                failure_not_before = 0.0
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log(f"[Silicon] Error: {exc}")
                failure_count += 1
                failure_not_before = time.monotonic() + min(
                    30.0,
                    float(2 ** min(failure_count - 1, 5)),
                )
            now = time.monotonic()
            timeout = schedule.seconds_until_due(
                now,
                eligible=eligible_handlers,
            )
            if not maintenance_active:
                timeout = min(
                    timeout,
                    max(0.0, next_contact_integrity - now),
                )
            call_deadline = next_inactive_call_deadline()
            if call_deadline is not None:
                timeout = min(
                    timeout,
                    max(0.0, call_deadline - time.time()),
                )
            # Native notifications cover every cross-process runtime source.
            # The minute cap is recovery-only; platforms without a healthy
            # native watcher retain the original half-second polling safety.
            fallback = 60.0 if runtime_file_notifications_active() else 0.5
            activity_ready = wait_for_runtime_activity(min(timeout, fallback))
    except KeyboardInterrupt:
        log("\n[Silicon] Shutting down.")
        raise SystemExit(0)
    finally:
        stop_runtime_health()
        stop_runtime_file_watch()
        stop_listener()
        dispatcher.shutdown(wait=False)


def run_headed_browser():
    """Open headed browser via silicon-browser for manual login.
    silicon-browser has built-in stealth and bundles its own browser."""
    from worker.handler import SILICON_BROWSER_PROFILE

    log("[Silicon] Opening headed browser for login")
    log(f"[Silicon] Profile: {SILICON_BROWSER_PROFILE}")
    log("[Silicon] Log into any services you need. Press Ctrl+C when done.")
    log("")

    cmd = [
        "silicon-browser",
        "--profile", SILICON_BROWSER_PROFILE,
        "--headed",
        "open", "https://google.com",
    ]

    try:
        subprocess.run(cmd)
        log("[Silicon] Browser open. Log into your services, then press Ctrl+C to save and close.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("\n[Silicon] Closing browser and saving profile...")
        subprocess.run([
            "silicon-browser",
            "--profile", SILICON_BROWSER_PROFILE,
            "close",
        ], capture_output=True)
        log("[Silicon] Profile saved. Login state persisted for browser workers.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "diag":
        from diagnostics.cli import main as diag_main

        raise SystemExit(diag_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in {"update", "update-check"}:
        from interface.release.updater import main as update_main

        raise SystemExit(update_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "maintenance":
        from manager.runtime.maintenance import main as maintenance_main

        raise SystemExit(maintenance_main(["--root", PROJECT_ROOT, *sys.argv[2:]]))

    # Living files ship in prompts/ and are read in place. Source updates are
    # owned exclusively by the offline silicon-cli update flow; runtime boot
    # never fetches Git or mutates repository configuration.
    try:
        from interface.backup import ensure_manifest_file

        archived = ensure_manifest_file(PROJECT_ROOT)
        if archived:
            log(
                "[Silicon] Archived legacy backup directory: "
                + ", ".join(archived)
            )
    except Exception as e:
        log(f"[Silicon] backup manifest check skipped: {e}")

    # Install `iwantto` before any manager or worker can run. It is rewritten
    # every boot so it always points at the active source generation.
    try:
        from diagnostics.iwantto.launcher import install as install_iwantto

        log(f"[Silicon] iwantto installed at {install_iwantto()}")
    except Exception as e:
        log(f"[Silicon] iwantto launcher install failed: {e}")

    # Glass is the single source of truth for provider API keys — pull them into
    # the environment before the brain CLIs or any browser subprocess run, so
    # nothing has to be stored locally on this box.
    try:
        from interface.config import load_provider_keys_into_env

        loaded = load_provider_keys_into_env()
        if loaded:
            log(f"[Silicon] Loaded {len(loaded)} provider key(s) from Glass: {', '.join(sorted(loaded))}")
        else:
            log("[Silicon] No provider keys returned by Glass (using existing environment).")
    except Exception as e:  # boot must not fail on key fetch
        log(f"[Silicon] Provider key load skipped: {e}")

    if len(sys.argv) > 1 and sys.argv[1] == "browser":
        run_headed_browser()
    else:
        main()
