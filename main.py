import atexit
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
from core.runtime_paths import DATA_ROOT

PROJECT_ROOT = os.fspath(DATA_ROOT)
LOCAL_BIN = os.path.join(PROJECT_ROOT, ".local", "bin")
ACTIVE_ENV_BIN = os.path.dirname(os.path.abspath(sys.executable))
_path_entries = [
    entry
    for entry in os.environ.get("PATH", "").split(os.pathsep)
    if entry and entry not in {ACTIVE_ENV_BIN, LOCAL_BIN}
]
# Package entry points belong to the selected generation environment. Keep that
# bin directory ahead of the legacy data-root bin so an old generated Extend
# launcher can never shadow the installed silicon-extend command.
os.environ["PATH"] = os.pathsep.join(
    [ACTIVE_ENV_BIN, LOCAL_BIN, *_path_entries]
)

from config import EVENT_LOOP, LOOP_TICK
from manager import manager_code, parse_manager_output, new_session, _is_rate_limit, TIMEOUT_MSG
from core.interface import (
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
    stop_listener,
    take_back_event,
    validate_contacts_integrity,
    wait_for_runtime_activity,
)
from core.messages import send_manager_message
from core.cron import execute_cron_tool
from core.maintenance import (
    COORDINATOR as MAINTENANCE,
    RootAdmission,
    heartbeat_scope,
)
from core.runtime_health import start_runtime_health, stop_runtime_health
from core.extend import (
    execute_tool as execute_extend_tool,
    inspect_extend_for_manager,
    request_setup as request_extend_setup,
)
from worker.handler import (
    start_worker,
    message_worker,
    get_worker_status,
    stop_worker,
    list_active,
    list_archive,
    read_archive,
    reconcile_maintenance_activities,
)
from core.cron.checkback import add_checkback
from core.diagnostics import Diagnostics
from core.progress import (
    contains_advertising_memory_reference,
    contains_private_manager_tool,
    diagnostic_error_summary,
    progress_is_error,
    redact_diagnostic_text,
)
from core.work_updates import (
    begin_manager_activity,
    current_manager_activity_group,
    enqueue_outbound_call,
    execute_work_update,
    prepare_outbound_call,
    record_worker_started,
    set_active_task_timer,
    settle_manager_activity,
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
        from core.team_context import reconcile_team_context

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
        from core.trust import reconcile_trust_policy

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


def _make_provider_progress_handler(carbon_id, group):
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


_EXTEND_DISCOVERY_ACTIONS = {
    "list",
    "ready",
    "needs_setup",
    "pending",
    "status",
    "show",
    "connections",
    "requests",
}
_EXTEND_ACTION_ALIASES = {
    "tools": "list",
    "run": "execute",
    "setup": "request_setup",
}


def _parse_extend_tool(tool_spec):
    """Return a normalized Extend action and tool key."""

    tool_name = str(tool_spec.get("tool") or "")
    suffix = (
        tool_name.removeprefix("extend/")
        if tool_name.startswith("extend/")
        else ""
    )
    explicit_action = (
        str(tool_spec.get("type") or "").strip().lower().replace("-", "_")
    )
    action_names = (
        _EXTEND_DISCOVERY_ACTIONS
        | {"execute", "request_setup"}
        | set(_EXTEND_ACTION_ALIASES)
    )
    if explicit_action:
        action = explicit_action
    elif suffix in action_names:
        action = suffix
    else:
        action = "execute"
    action = _EXTEND_ACTION_ALIASES.get(action, action)

    key = str(tool_spec.get("name") or tool_spec.get("key") or "").strip()
    if not key and suffix and suffix not in action_names:
        key = suffix
    return action, key


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

    if tool_name == "extend" or tool_name.startswith("extend/"):
        action, key = _parse_extend_tool(tool_spec)
        if action == "request_setup":
            return f"requesting Extend setup: {key or 'unknown'}"
        if action == "list":
            return "listing team-enabled Extend tools"
        if action == "ready":
            return "listing ready Extend tools"
        if action == "needs_setup":
            return "checking which Extend tools need setup"
        if action == "pending":
            return "checking pending Extend setup"
        if action == "status":
            return "checking Extend status"
        if action == "show":
            return f"inspecting Extend tool: {key or 'unknown'}"
        if action in {"connections", "requests"}:
            return f"checking Extend {action}"
        return f"called Extend tool: {key or 'unknown'}"

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
        or tool_name == "extend"
        or tool_name.startswith("extend/")
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


def execute_single_tool(tool_spec, carbon_id):
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
                result = _execute_single_tool(tool_spec, carbon_id)
                executed = True
                result_status = "error" if "Error" in str(result) else "ok"
                result_summary = (
                    "[Extend result omitted]"
                    if tool_name == "extend" or tool_name.startswith("extend/")
                    else "[Advertising memory result omitted]"
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
                result = _execute_single_tool(tool_spec, carbon_id)
    else:
        result = _execute_single_tool(tool_spec, carbon_id)
    if tool_name and tool_name != "do_nothing":
        try:
            from core.activity_log import tool_call
            tool_call(carbon_id, tool_name, tool_spec, result)
        except Exception:
            pass
    return result


def _execute_single_tool(tool_spec, carbon_id):
    """Execute a single tool. Returns result string or None for do_nothing."""
    tool_name = tool_spec.get("tool", "")

    if tool_name == "do_nothing":
        return None

    progress_note = _tool_progress_note(tool_spec)
    if progress_note:
        group = _manager_progress_group(carbon_id)
        if group:
            send_progress(
                carbon_id,
                group,
                _tool_progress_state(tool_spec),
                progress_note,
            )

    if tool_name == "work_update":
        return f"Tool 'work_update': {execute_work_update(tool_spec, carbon_id)}"

    if tool_name == "reply":
        message = tool_spec.get("message", "")
        status = reply_user(
            message,
            carbon_id,
            work_continues=bool(tool_spec.get("work_continues", False)),
        )
        return f"Tool 'reply': {status}"

    elif tool_name == "message_manager":
        target_carbon_id = tool_spec.get("carbon_id", "")
        target_silicon_id = tool_spec.get("silicon_id", "")
        message = tool_spec.get("message", "")
        if not message:
            return "Tool 'message_manager': Error: message is required"

        if target_carbon_id:
            try:
                contact = ensure_contact_for_target("carbon", target_carbon_id)
            except Exception as e:
                status = _message_failure_status(carbon_id, "carbon", target_carbon_id, e)
                return f"Tool 'message_manager' (to {target_carbon_id}): Error: {status}"
            target_id = contact.get("carbon_id") or target_carbon_id
            target_name = (
                f"{contact.get('display_name') or contact.get('name') or target_id}'s manager"
            )
            try:
                work_call = prepare_outbound_call(
                    carbon_id,
                    target_kind="manager",
                    target_id=target_id,
                    target_name=target_name,
                    message=message,
                    task_id=str(tool_spec.get("task_id") or ""),
                )
            except Exception:
                work_call = {}
            status = send_manager_message(
                carbon_id,
                target_id,
                message,
                target_type="carbon",
                work_call=work_call,
            )
            work_call_accepted = False
            try:
                work_call_accepted = bool(
                    enqueue_outbound_call(
                        work_call,
                        target_name=target_name,
                        message=message,
                    )
                )
            except Exception:
                pass
            return (
                f"Tool 'message_manager' (to {target_id}): {status}"
                + _work_reference_suffix(
                    work_call if work_call_accepted else {},
                    "task_id",
                    "work_event_id",
                    "call_id",
                )
            )

        if target_silicon_id:
            try:
                contact = ensure_contact_for_target("silicon", target_silicon_id)
            except Exception as e:
                status = _message_failure_status(carbon_id, "silicon", target_silicon_id, e)
                return f"Tool 'message_manager' (to {target_silicon_id}): Error: {status}"
            target_id = contact.get("silicon_id") or target_silicon_id
            target_name = str(
                contact.get("display_name")
                or contact.get("name")
                or target_id
            )
            try:
                work_call = prepare_outbound_call(
                    carbon_id,
                    target_kind="silicon",
                    target_id=target_id,
                    target_name=target_name,
                    message=message,
                    task_id=str(tool_spec.get("task_id") or ""),
                )
            except Exception:
                work_call = {}
            status = send_manager_message(
                carbon_id,
                target_id,
                message,
                target_type="silicon",
                work_call=work_call,
            )
            work_call_accepted = False
            try:
                work_call_accepted = bool(
                    enqueue_outbound_call(
                        work_call,
                        target_name=target_name,
                        message=message,
                    )
                )
            except Exception:
                pass
            return (
                f"Tool 'message_manager' (to {target_id}): {status}"
                + _work_reference_suffix(
                    work_call if work_call_accepted else {},
                    "task_id",
                    "work_event_id",
                    "call_id",
                )
            )

        return "Tool 'message_manager': Error: carbon_id or silicon_id is required"

    elif tool_name in {"trust/list", "trust/get"}:
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
            from core.trust import inspect_trust_policy

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

    elif tool_name == "trust/set":
        target_carbon_id = str(tool_spec.get("carbon_id") or "").strip()
        target_silicon_id = str(tool_spec.get("silicon_id") or "").strip()
        if bool(target_carbon_id) == bool(target_silicon_id):
            return (
                "Tool 'trust/set': Error: provide exactly one of carbon_id "
                "or silicon_id"
            )
        raw_level = tool_spec.get("level")
        level = None if raw_level in {None, "", "inherit", "team_default"} else str(raw_level)
        try:
            from core.trust import set_contact_trust

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

    elif tool_name == "remote_browser":
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

    elif tool_name == "take_back":
        request_id = tool_spec.get("request_id", "")
        event_id = tool_spec.get("event_id", "")
        if request_id:
            status = complete_take_back(request_id, tool_spec.get("message", ""))
            return f"Tool 'take_back': {status}"
        if event_id:
            status = take_back_event(event_id, reason=tool_spec.get("reason", ""), force=bool(tool_spec.get("force", False)))
            return f"Tool 'take_back': {status}"
        return "Tool 'take_back': Error: request_id or event_id is required"

    elif tool_name == "advertising_memory/update":
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
            from core.team_context import update_own_advertising_memory

            outcome = update_own_advertising_memory(
                content,
                root=PROJECT_ROOT,
                resolve_conflict=resolve_conflict,
            )
        except ValueError as exc:
            return f"Tool 'advertising_memory/update': Error: {exc}"
        except Exception as exc:
            return f"Tool 'advertising_memory/update': Error: {exc}"

        if isinstance(outcome, dict):
            status = str(outcome.get("status") or "saved")
            details = []
            revision = outcome.get("revision")
            actual_revision = outcome.get("actual_revision")
            if isinstance(revision, int) and not isinstance(revision, bool):
                details.append(f"revision {revision}")
            if (
                isinstance(actual_revision, int)
                and not isinstance(actual_revision, bool)
            ):
                details.append(f"Glass is at revision {actual_revision}")
            if outcome.get("local_saved") and outcome.get("ok") is False:
                details.append("local draft preserved")
            detail = str(outcome.get("detail") or "").strip()
            if detail:
                details.append(detail)
            suffix = f" — {'; '.join(details)}" if details else ""
            error_prefix = "Error: " if outcome.get("ok") is False else ""
            return (
                "Tool 'advertising_memory/update': "
                f"{error_prefix}{status}{suffix}"
            )
        return f"Tool 'advertising_memory/update': {outcome or 'saved'}"

    elif tool_name == "extend" or tool_name.startswith("extend/"):
        action, key = _parse_extend_tool(tool_spec)
        if action == "request_setup":
            return request_extend_setup(
                key,
                note=tool_spec.get("note", ""),
                carbon_id=carbon_id,
            )
        if action in _EXTEND_DISCOVERY_ACTIONS:
            return inspect_extend_for_manager(
                action,
                tool_key=key,
                query=tool_spec.get("query", ""),
                page=tool_spec.get("page", 1),
                limit=tool_spec.get("limit", 100),
                status=tool_spec.get("status", ""),
            )
        if action != "execute":
            return f"Tool 'extend/{key or 'unknown'}': Error: unknown type '{action}'"
        arguments = tool_spec.get("arguments", {})
        return execute_extend_tool(key, arguments, carbon_id=carbon_id)

    elif tool_name.startswith("cron/"):
        try:
            return execute_cron_tool(tool_spec)
        except Exception as e:
            return f"Tool '{tool_name}': Error: {e}"

    elif tool_name == "cron/list":
        try:
            return execute_cron_tool(tool_spec)
        except Exception as e:
            return f"Tool 'cron/list': Error: {e}"

    elif tool_name.startswith("worker"):
        worker_type, action_type, worker_id = _parse_worker_tool(tool_spec)

        if action_type == "new":
            if not worker_type:
                return "Tool 'worker/new': Error: worker_type is required. Use worker/browser, worker/terminal, or worker/writer"
            if not worker_id:
                return "Tool 'worker/new': Error: worker-id is required"
            task = tool_spec.get("task", "")
            if not task:
                return f"Tool 'worker/new' ({worker_id}): Error: task is required"
            incognito = tool_spec.get("incognito", False)
            status = start_worker(worker_id, task, worker_type, carbon_id, incognito=incognito)
            work_invocation = {}
            if "Error" not in status:
                work_invocation = record_worker_started(
                    carbon_id,
                    worker_id,
                    worker_type,
                    task,
                    queued="queued" in status.lower(),
                    task_id=str(tool_spec.get("task_id") or ""),
                )
                trace = Diagnostics.get_active_run(carbon_id)
                if trace is not None:
                    trace.note_worker_spawned()
                    trace.event("worker.spawned", worker_id=worker_id, worker_type=worker_type)

            # Handle checkback_in if specified
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

        elif action_type == "message":
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

        elif action_type == "checkback":
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

        elif action_type == "status":
            status = get_worker_status(worker_id, carbon_id)
            return f"Tool 'worker/status' ({worker_id}): {status}"

        elif action_type == "stop":
            status = stop_worker(worker_id, carbon_id)
            return f"Tool 'worker/stop' ({worker_id}): {status}"

        elif action_type == "list_active":
            status = list_active(carbon_id)
            return f"Tool 'worker/list_active': {status}"

        elif action_type == "list_archive":
            status = list_archive(carbon_id)
            return f"Tool 'worker/list_archive': {status}"

        elif action_type == "read_archive":
            output = read_archive(worker_id, carbon_id)
            return f"Tool 'worker/read_archive' ({worker_id}): {output}"

        else:
            return f"Tool 'worker': Unknown type '{action_type}'"

    elif tool_name == "new_session":
        new_id = new_session(carbon_id)
        return f"Tool 'new_session': Done. New session id: {new_id}"

    elif tool_name == "restart_silicon_service":
        # Handled separately in execute_all_tools
        return None

    else:
        return f"Unknown tool: '{tool_name}'"


def execute_all_tools(all_tools):
    """Execute all tools from all managers through a single executor.
    all_tools is a list of (carbon_id, tool_spec) tuples.
    Returns (results_by_carbon, empty_remaps_for_legacy_callers)."""
    results_by_carbon = {}
    needs_restart = False
    restart_carbon_id = None

    # Sort: restart at end
    restart_tools = [(cid, t) for cid, t in all_tools if t.get("tool") == "restart_silicon_service"]
    other_tools = [(cid, t) for cid, t in all_tools if t.get("tool") != "restart_silicon_service"]
    sorted_tools = other_tools + restart_tools

    for carbon_id, tool_spec in sorted_tools:
        tool_name = tool_spec.get("tool", "")

        if tool_name == "restart_silicon_service":
            needs_restart = True
            restart_carbon_id = carbon_id
            continue

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
        if str(tool_spec.get("tool") or "") == "extend"
        or str(tool_spec.get("tool") or "").startswith("extend/")
        or str(tool_spec.get("tool") or "") == "advertising_memory/update"
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
        str(tool_spec.get("tool") or "") == "extend"
        or str(tool_spec.get("tool") or "").startswith("extend/")
        or str(tool_spec.get("tool") or "") == "advertising_memory/update"
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
    selected = set(handler_names or ())

    for handler in EVENT_LOOP:
        if selected and handler["name"] not in selected:
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
            on_error = handler.get("on_error")
            if on_error:
                try:
                    on_error(str(e))
                except Exception:
                    pass

    # Merge context lists into strings
    merged = {}
    for carbon_id, parts in context_by_carbon.items():
        merged[carbon_id] = "\n\n".join(parts)

    return merged


def _make_mid_stream_handler(carbon_id):
    """Create a callback that executes reply tools mid-stream for fast delivery.
    Only explicitly intermediate replies are fire-and-forget. Final replies
    stay in the ordered executor so a terminal card or other preceding update
    is accepted first. All non-reply tools need their results fed back to the
    manager and therefore also use the centralized executor."""
    def on_tools(tools_list):
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


def _instrumented_manager_call(carbon_id, text, trace, iteration, on_tools, on_progress):
    if trace is None:
        return manager_code(text, carbon_id, on_tools=on_tools, on_progress=on_progress)
    with trace.span(f"round[{iteration}]"):
        with trace.span("manager_turn"):
            return manager_code(
                text, carbon_id, on_tools=on_tools, on_progress=on_progress,
                trace=trace,
            )


def run_all_managers(context_by_carbon):
    """Run managers and retain one complete graph for each inbound message batch."""
    # Commands are roots too. They must cross the same durable update fence as
    # ordinary manager turns, rather than running during ingestion.
    pending = handle_commands(dict(context_by_carbon))
    if not pending:
        return
    max_iterations = 10
    traces = {}
    activity_groups = {}

    def get_trace(carbon_id, context=""):
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

    def close_trace(carbon_id):
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

    try:
        for iteration in range(max_iterations):
            if not pending:
                break

            log(f"[Silicon] Manager round {iteration + 1} for {list(pending.keys())}...")
            manager_outputs = {}
            already_executed = {}
            with ThreadPoolExecutor(max_workers=max(len(pending), 1)) as executor:
                futures = {}
                for carbon_id, text in pending.items():
                    trace = get_trace(carbon_id, text)
                    group = activity_groups.get(carbon_id)
                    visible_activity = (
                        trace is None
                        or str(getattr(trace, "trigger", "") or "") == "message"
                    )
                    if not group and visible_activity:
                        group = begin_manager_activity(
                            carbon_id,
                            getattr(trace, "run_id", "") if trace is not None else "",
                        )
                        activity_groups[carbon_id] = group
                    group = group or ""
                    set_active_task_timer(
                        carbon_id,
                        timer_state="running",
                    )
                    on_tools = _make_mid_stream_handler(carbon_id)
                    on_progress = _make_provider_progress_handler(carbon_id, group)
                    if group:
                        send_progress(
                            carbon_id,
                            group,
                            "thinking",
                            "calling manager",
                            frame_key=f"manager:round:{iteration}",
                        )
                    future = executor.submit(
                        _instrumented_manager_call, carbon_id, text, trace,
                        iteration, on_tools, on_progress,
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
                        set_active_task_timer(
                            carbon_id,
                            timer_state="paused",
                            pause_reason="infrastructure",
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
                    if output and _is_rate_limit(output):
                        set_active_task_timer(
                            carbon_id,
                            timer_state="paused",
                            pause_reason="rate_limited",
                        )
                        reply_user(_rate_limit_reply_text(output), carbon_id)
                        continue
                    if output == TIMEOUT_MSG:
                        pending[carbon_id] = output
                        continue
                    if not output or not output.strip():
                        error_msg = "Manager must output TOOL JSON. You returned empty output."
                    elif '"tools"' not in output and "'tools'" not in output:
                        error_msg = "Manager must output TOOL JSON. No tools key found in your output."
                    else:
                        error_msg = "TOOL JSON formatting is incorrect. Could not parse valid JSON."
                    pending[carbon_id] = error_msg
                    continue

                if is_only_do_nothing(tools_data):
                    continue
                executed_keys = {
                    json.dumps(t, sort_keys=True)
                    for t in already_executed.get(carbon_id, [])
                }
                for tool_spec in tools_data["tools"]:
                    key = json.dumps(tool_spec, sort_keys=True)
                    if tool_spec.get("tool") != "do_nothing" and key not in executed_keys:
                        all_tools.append((carbon_id, tool_spec))

            if all_tools:
                results_by_carbon, remaps = execute_all_tools(all_tools)
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
                for carbon_id, results in results_by_carbon.items():
                    if results:
                        pending[carbon_id] = "Tool execution results:\n" + "\n".join(results)

            for carbon_id in [cid for cid in traces if cid not in pending]:
                close_trace(carbon_id)

        if pending:
            log(f"[Silicon] Max manager iterations reached. Remaining: {list(pending.keys())}")
            for carbon_id in pending:
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
        self._running = set()
        self._threads = set()
        self._closed = False
        self._slots = threading.BoundedSemaphore(max(1, int(max_active_contacts)))

    def submit(self, context_by_carbon):
        """Durably enqueue roots and start only those admitted before the fence."""
        admissions = []
        for carbon_id, context in (context_by_carbon or {}).items():
            if not context:
                continue
            result = MAINTENANCE.enqueue_root(carbon_id, str(context))
            if result.admission is not None:
                admissions.append(result.admission)
        self._schedule_admissions(admissions)

    def _schedule_admissions(self, admissions):
        started = []
        with self._condition:
            if self._closed:
                raise RuntimeError("manager dispatcher is closed")
            for admission in admissions:
                if not isinstance(admission, RootAdmission):
                    continue
                carbon_id = admission.contact_id
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
                            self._running.discard(carbon_id)
                            self._threads.discard(threading.current_thread())
                            released = True
                            self._condition.notify_all()
                            return
                    try:
                        # One contact's accepted roots may be coalesced into a
                        # turn, but every durable claim remains leased until
                        # that turn has actually returned.
                        with heartbeat_scope(
                            [item.activity for item in admissions],
                            coordinator=MAINTENANCE,
                        ):
                            self._runner({
                                carbon_id: "\n\n".join(
                                    item.context for item in admissions
                                )
                            })
                        MAINTENANCE.complete_roots(admissions)
                        log(f"[Silicon] Manager loop complete for {carbon_id}.")
                    except Exception as exc:
                        MAINTENANCE.retry_roots(admissions)
                        log(f"[Silicon] Manager dispatcher error for {carbon_id}: {exc}")
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
            from core.background import flush_best_effort

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


def main():
    _install_diagnostic_shutdown_hooks()
    log("[Silicon] Starting event loop...")
    log(f"[Silicon] Periodic tick interval: {LOOP_TICK}s; Interface wakeups are immediate")

    _bootstrap_team_context()
    _bootstrap_trust_policy()
    start_listener()
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

    next_periodic = 0.0
    immediate_handlers = {"check_interface", "check_manager_messages"}
    try:
        while True:
            try:
                maintenance_status = _maintenance_runtime_tick(
                    dispatcher,
                    attest=False,
                )
                maintenance_active = (
                    maintenance_status.get("phase") != "available"
                )
                now = time.monotonic()
                periodic_due = now >= next_periodic
                if periodic_due and not maintenance_active:
                    validate_contacts_integrity()

                if maintenance_active:
                    selected_handlers = {
                        "check_interface",
                        "check_manager_messages",
                        "check_workers",
                    }
                else:
                    selected_handlers = (
                        None if periodic_due else immediate_handlers
                    )
                context_by_carbon = run_event_loop_tick(selected_handlers)
                if periodic_due:
                    next_periodic = time.monotonic() + LOOP_TICK

                if context_by_carbon:
                    for cid, ctx in context_by_carbon.items():
                        log(f"[Silicon] Context for {cid}:\n{ctx[:200]}...")
                    dispatcher.submit(context_by_carbon)
                _maintenance_runtime_tick(dispatcher, attest=True)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log(f"[Silicon] Error: {exc}")
                if next_periodic <= time.monotonic():
                    next_periodic = time.monotonic() + min(float(LOOP_TICK), 1.0)
            timeout = max(0.0, next_periodic - time.monotonic())
            # Maintenance requests are raised by silicon-cli in another
            # process, so cap the sleep even when no Interface event arrives.
            wait_for_runtime_activity(min(timeout, 0.5))
    except KeyboardInterrupt:
        log("\n[Silicon] Shutting down.")
        raise SystemExit(0)
    finally:
        stop_runtime_health()
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
        from core.diag_cli import main as diag_main

        raise SystemExit(diag_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in {"update", "update-check"}:
        from update import main as update_main

        raise SystemExit(update_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "maintenance":
        from core.maintenance import main as maintenance_main

        raise SystemExit(maintenance_main(["--root", PROJECT_ROOT, *sys.argv[2:]]))

    # Seed missing living files from tracked templates. Source updates are owned
    # exclusively by the offline silicon-cli update flow; runtime boot never
    # fetches Git or mutates repository configuration.
    try:
        from core.backup import ensure_manifest_file
        from core.living_files import seed_living_files

        archived = ensure_manifest_file(PROJECT_ROOT)
        if archived:
            log(
                "[Silicon] Archived legacy backup directory: "
                + ", ".join(archived)
            )
        seeded = seed_living_files()
        if seeded:
            log(f"[Silicon] Seeded {len(seeded)} living file(s) from templates.")
    except Exception as e:
        log(f"[Silicon] living-file seed skipped: {e}")

    # Glass is the single source of truth for provider API keys — pull them into
    # the environment before the brain CLIs or any browser subprocess run, so
    # nothing has to be stored locally on this box.
    try:
        from core.glass import load_provider_keys_into_env

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
