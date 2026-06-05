import time
import sys
import os
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure project root is in path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import EVENT_LOOP, LOOP_TICK
from manager import manager_code, parse_manager_output, new_session, _is_rate_limit, TIMEOUT_MSG
from core.interface import (
    complete_take_back,
    ensure_contact_for_target,
    get_contacts,
    remote_browser_close,
    remote_browser_share,
    reply_contact as reply_user,
    send_progress,
    take_back_event,
    validate_contacts_integrity,
)
from core.messages import send_manager_message
from core.cron import execute_cron_tool
from worker.handler import (
    start_worker,
    message_worker,
    get_worker_status,
    stop_worker,
    list_active,
    list_archive,
    read_archive,
)
from core.cron.checkback import add_checkback

RESTART_FLAG = os.path.join(PROJECT_ROOT, ".restart_pending")


def log(msg):
    print(msg, flush=True)


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


def execute_single_tool(tool_spec, carbon_id):
    """Execute a single tool. Returns result string or None for do_nothing."""
    tool_name = tool_spec.get("tool", "")

    if tool_name == "do_nothing":
        return None

    elif tool_name == "reply":
        message = tool_spec.get("message", "")
        status = reply_user(message, carbon_id)
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
                return f"Tool 'message_manager' (to {target_carbon_id}): Error: {e}"
            target_id = contact.get("carbon_id") or target_carbon_id
            status = send_manager_message(carbon_id, target_id, message)
            return f"Tool 'message_manager' (to {target_id}): {status}"

        if target_silicon_id:
            try:
                contact = ensure_contact_for_target("silicon", target_silicon_id)
            except Exception as e:
                return f"Tool 'message_manager' (to {target_silicon_id}): Error: {e}"
            target_id = contact.get("silicon_id") or target_silicon_id
            status = send_manager_message(carbon_id, target_id, message)
            return f"Tool 'message_manager' (to {target_id}): {status}"

        return "Tool 'message_manager': Error: carbon_id or silicon_id is required"

    elif tool_name == "remote_browser":
        action_type = tool_spec.get("type", "share")
        if action_type == "share":
            expiry = tool_spec.get("expiry", 60)
            new = tool_spec.get("new", True)
            status = remote_browser_share(carbon_id, expiry=expiry, new=new)
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
            task = tool_spec.get("task", "")
            incognito = tool_spec.get("incognito", False)
            status = start_worker(worker_id, task, worker_type, carbon_id, incognito=incognito)
            send_progress(carbon_id, f"worker:{worker_id}", "executing", status)

            # Handle checkback_in if specified
            checkback_in = tool_spec.get("checkback_in")
            if checkback_in and "Error" not in status:
                try:
                    add_checkback(worker_id, carbon_id, float(checkback_in))
                    status += f" (checkback in {checkback_in} min)"
                except Exception as e:
                    status += f" (checkback setup failed: {e})"

            return f"Tool 'worker/new' ({worker_type}, {worker_id}): {status}"

        elif action_type == "message":
            task = tool_spec.get("message", "")
            if not worker_id:
                return "Tool 'worker/message': Error: worker-id is required"
            if not task:
                return f"Tool 'worker/message' ({worker_id}): Error: message is required"
            status = message_worker(worker_id, task, carbon_id)
            send_progress(carbon_id, f"worker:{worker_id}", "executing", status)
            return f"Tool 'worker/message' ({worker_id}): {status}"

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
            send_progress(carbon_id, f"worker:{worker_id}", "done", status)
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
    return cleaned


# --- Event loop ---

def run_event_loop_tick():
    """Run one tick of the event loop. Returns {carbon_id: context_string}."""
    context_by_carbon = {}

    for handler in EVENT_LOOP:
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
                        if handler["name"] == "check_workers":
                            send_progress(carbon_id, "workers", "done", "worker update ready")
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
    Only reply is fire-and-forget. All other tools need their results fed back
    to the manager, so they must go through the centralized executor."""
    def on_tools(tools_list):
        succeeded = []
        for tool_spec in tools_list:
            tool_name = tool_spec.get("tool", "")
            if tool_name != "reply":
                continue
            result = execute_single_tool(tool_spec, carbon_id)
            if result:
                log(f"[Silicon] Mid-stream: {result}")
                if "Error" not in result:
                    succeeded.append(tool_spec)
        return succeeded
    return on_tools


def run_all_managers(context_by_carbon):
    """Run managers for all carbons that have context. Parallel invocation, centralized tool execution."""
    pending = dict(context_by_carbon)  # {carbon_id: text_to_send}
    max_iterations = 10

    for iteration in range(max_iterations):
        if not pending:
            break

        log(f"[Silicon] Manager round {iteration + 1} for {list(pending.keys())}...")

        # Invoke all managers in parallel
        manager_outputs = {}       # {carbon_id: raw_text}
        already_executed = {}      # {carbon_id: [tool_spec, ...]}
        with ThreadPoolExecutor(max_workers=max(len(pending), 1)) as executor:
            futures = {}
            for carbon_id, text in pending.items():
                on_tools = _make_mid_stream_handler(carbon_id)
                send_progress(carbon_id, f"manager:{carbon_id}", "thinking", "manager running")
                future = executor.submit(manager_code, text, carbon_id, on_tools=on_tools)
                futures[future] = carbon_id

            for future in as_completed(futures):
                carbon_id = futures[future]
                try:
                    output, _, executed_tools = future.result()
                    manager_outputs[carbon_id] = output
                    if executed_tools:
                        already_executed[carbon_id] = executed_tools
                    send_progress(carbon_id, f"manager:{carbon_id}", "done", "manager finished")
                except Exception as e:
                    manager_outputs[carbon_id] = f'{{"tools": [{{"tool": "reply", "message": "Manager error: {e}"}}, {{"tool": "do_nothing"}}]}}'
                    send_progress(carbon_id, f"manager:{carbon_id}", "done", f"manager error: {e}")

        # Parse tools from all managers
        all_tools = []  # list of (carbon_id, tool_spec)
        pending = {}

        for carbon_id, output in manager_outputs.items():
            log(f"[Silicon] Manager output for {carbon_id}: {output[:200]}...")

            tools_data = parse_manager_output(output)

            if tools_data is None:
                # Check if this is a rate limit message — notify user, don't retry
                if output and _is_rate_limit(output):
                    log(f"[Silicon] Rate limit for {carbon_id}: {output[:200]}")
                    reply_user(output.strip(), carbon_id)
                    continue

                # Manager timed out — feed the timeout message back directly
                if output == TIMEOUT_MSG:
                    log(f"[Silicon] Manager timeout for {carbon_id}")
                    pending[carbon_id] = output
                    continue

                if not output or not output.strip():
                    error_msg = "Manager must output TOOL JSON. You returned empty output."
                elif '"tools"' not in output and "'tools'" not in output:
                    error_msg = "Manager must output TOOL JSON. No tools key found in your output."
                else:
                    error_msg = "TOOL JSON formatting is incorrect. Could not parse valid JSON from your output. Make sure the JSON is valid."
                log(f"[Silicon] Parse error for {carbon_id}: {error_msg}")
                pending[carbon_id] = error_msg
                continue

            if is_only_do_nothing(tools_data):
                log(f"[Silicon] Manager for {carbon_id} returned do_nothing.")
                continue

            # Dedup against tools already executed mid-stream
            executed_keys = set()
            if carbon_id in already_executed:
                executed_keys = {json.dumps(t, sort_keys=True) for t in already_executed[carbon_id]}

            for tool_spec in tools_data["tools"]:
                if tool_spec.get("tool") == "do_nothing":
                    continue
                if json.dumps(tool_spec, sort_keys=True) in executed_keys:
                    log(f"[Silicon] Skipping (already mid-stream): {tool_spec.get('tool')}")
                    continue
                all_tools.append((carbon_id, tool_spec))

        # Execute all tools through centralized executor
        if all_tools:
            results_by_carbon, remaps = execute_all_tools(all_tools)
            log(f"[Silicon] Tool results: {results_by_carbon}")

            # Apply carbon_id remaps to pending
            for old_id, new_id in remaps.items():
                if old_id in pending:
                    pending[new_id] = pending.pop(old_id)

            for carbon_id, results in results_by_carbon.items():
                if results:
                    pending[carbon_id] = "Tool execution results:\n" + "\n".join(results)

    if pending:
        log(f"[Silicon] Max manager iterations reached. Remaining: {list(pending.keys())}")


def main():
    log("[Silicon] Starting event loop...")
    log(f"[Silicon] Tick interval: {LOOP_TICK}s")

    # Check if we just restarted
    restart_msg, restart_carbon_id = _check_restart_flag()
    if restart_msg:
        if restart_carbon_id:
            log(f"[Silicon] Post-restart for {restart_carbon_id}: {restart_msg}")
            run_all_managers({restart_carbon_id: restart_msg})
        else:
            # Find central carbon to notify
            log(f"[Silicon] Post-restart (no carbon_id): {restart_msg}")
            contacts_data = get_contacts()
            for cid, info in contacts_data.get("contacts", {}).items():
                if info.get("is_central_carbon"):
                    run_all_managers({cid: restart_msg})
                    break

    while True:
        try:
            # Validate contacts integrity every tick
            validate_contacts_integrity()

            context_by_carbon = run_event_loop_tick()

            if context_by_carbon:
                context_by_carbon = handle_commands(context_by_carbon)

                if context_by_carbon:
                    for cid, ctx in context_by_carbon.items():
                        log(f"[Silicon] Context for {cid}:\n{ctx[:200]}...")
                    run_all_managers(context_by_carbon)
                    log("[Silicon] All manager loops done.")

        except KeyboardInterrupt:
            log("\n[Silicon] Shutting down.")
            sys.exit(0)
        except Exception as e:
            log(f"[Silicon] Error: {e}")

        time.sleep(LOOP_TICK)


def run_headed_browser():
    """Open headed browser via silicon-browser for manual login.
    silicon-browser has built-in stealth and bundles its own browser."""
    from worker.handler import SILICON_BROWSER_PROFILE

    log(f"[Silicon] Opening headed browser for login")
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
    if len(sys.argv) > 1 and sys.argv[1] == "browser":
        run_headed_browser()
    elif len(sys.argv) > 1 and sys.argv[1] in {"update", "update-check"}:
        from update import main as update_main

        raise SystemExit(update_main(sys.argv[2:]))
    else:
        main()
