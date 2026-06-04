import subprocess
import os
import json
import re
import time
import uuid
import platform
import shutil
import threading
import queue

from prompts.DNA import get_manager_prompt
from core.progress import (
    claude_progress_events,
    codex_progress_event,
    progress_display_line,
    write_progress_line,
)

IS_WINDOWS = platform.system() == "Windows"

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(PROJECT_ROOT, "sessions")
MANAGER_STREAMS_DIR = os.path.join(SESSIONS_DIR, "manager_streams")

# On Windows, find the full path to claude so we don't need shell=True
# (which has an 8191 char command line limit via cmd.exe)
CLAUDE_CMD = "claude"
CODEX_CMD = "codex"
if IS_WINDOWS:
    _claude_path = shutil.which("claude") or shutil.which("claude.cmd")
    if _claude_path:
        CLAUDE_CMD = _claude_path
    _codex_path = shutil.which("codex") or shutil.which("codex.cmd")
    if _codex_path:
        CODEX_CMD = _codex_path

TIMEOUT_MSG = "SYSTEM: You timed out (3 min limit). You were taking too long. Delegate long-running tasks to a worker instead of doing them yourself. If this task truly cannot be delegated, you may continue now — but be quick."


SILICON_CONFIG_FILE = os.path.join(PROJECT_ROOT, "silicon.json")


def _read_silicon_config():
    if not os.path.exists(SILICON_CONFIG_FILE):
        return {}
    try:
        with open(SILICON_CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {}


def get_brain():
    """Return the configured manager backend. Defaults to Claude for compatibility."""
    brain = _read_silicon_config().get("brain", "claude")
    if not isinstance(brain, str):
        return "claude"
    brain = brain.strip().lower()
    return brain if brain in {"claude", "codex"} else "claude"


def _session_file(carbon_id, brain="claude"):
    suffix = "" if brain == "claude" else f"_{brain}"
    return os.path.join(SESSIONS_DIR, f"{carbon_id}{suffix}.txt")


def _get_session_id(carbon_id):
    """Get session UUID for a specific carbon."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    session_file = _session_file(carbon_id, "claude")
    if os.path.exists(session_file):
        with open(session_file) as f:
            return f.read().strip()
    # Create a new session for this carbon
    return new_session(carbon_id, brain="claude")


def new_session(carbon_id, brain=None):
    """Reset the active manager session for a carbon."""
    brain = (brain or get_brain()).lower()
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    if brain == "codex":
        session_file = _session_file(carbon_id, "codex")
        if os.path.exists(session_file):
            os.remove(session_file)
        return "new codex thread will be created on next turn"

    new_id = str(uuid.uuid4())
    session_file = _session_file(carbon_id, "claude")
    with open(session_file, "w") as f:
        f.write(new_id)
    return new_id


def _write_prompt_file(carbon_id, prompt):
    """Write the system prompt to a file and return the path."""
    prompt_file = os.path.join(SESSIONS_DIR, f"{carbon_id}_prompt.md")
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(prompt)
    return prompt_file


def _is_rate_limit(text):
    """Check if text indicates an API rate limit."""
    lower = text.lower()
    return any(p in lower for p in [
        "rate limit", "rate_limit", "usage limit", "hit your limit",
        "too many requests", "quota exceeded", "overloaded",
    ])


def _display_stream_event(event, tag, state=None, progress_events=None):
    """Print a stream-json event to terminal."""
    progress_events = progress_events if progress_events is not None else claude_progress_events(event, state)
    if progress_events:
        for progress in progress_events:
            line = progress_display_line(progress)
            if line:
                print(f"  [{tag}] {line}", flush=True)
        return

    t = event.get("type", "")

    if t == "system" and event.get("subtype") == "init":
        model = event.get("model", "")
        sid = event.get("session_id", "")[:8]
        print(f"  [{tag}] session {sid} | {model}", flush=True)

    elif t == "assistant":
        content = event.get("message", {}).get("content", [])
        for block in content:
            bt = block.get("type", "")
            if bt == "text":
                txt = block.get("text", "").strip()
                if txt:
                    preview = txt[:150].replace("\n", " ")
                    if len(txt) > 150:
                        preview += "…"
                    print(f"  [{tag}] {preview}", flush=True)
            elif bt == "tool_use":
                name = block.get("name", "?")
                print(f"  [{tag}] tool: {name}", flush=True)

    elif t == "result":
        cost = event.get("cost_usd")
        duration = event.get("duration_ms")
        subtype = event.get("subtype", "")
        parts = [f"  [{tag}] done"]
        if subtype and subtype != "success":
            parts[0] += f" ({subtype})"
        if cost is not None:
            parts.append(f"${cost:.4f}")
        if duration is not None:
            parts.append(f"{duration / 1000:.1f}s")
        print(" ".join(parts), flush=True)


def _compact_preview(text, limit=180):
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        return text[:limit - 1] + "…"
    return text


def _codex_item_label(item):
    item_type = item.get("type", "")

    if item_type == "agentMessage":
        phase = item.get("phase")
        return f"assistant{f' ({phase})' if phase else ''}"

    if item_type == "commandExecution":
        command = item.get("command") or item.get("cmd") or item.get("argv") or ""
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return f"command: {_compact_preview(command, 120)}" if command else "command"

    if item_type == "mcpToolCall":
        name = item.get("name") or item.get("toolName") or item.get("serverName") or "mcp tool"
        return f"tool: {name}"

    if item_type == "fileChange":
        path = item.get("path") or item.get("filePath") or item.get("relativePath") or ""
        return f"file change: {path}" if path else "file change"

    if item_type == "userMessage":
        return "user message"

    if item_type:
        return item_type
    return "item"


def _display_codex_stream_event(msg, tag, state):
    """Print useful Codex app-server notifications as a live activity trace."""
    progress = codex_progress_event(msg, state)
    line = progress_display_line(progress)
    if line:
        print(f"  [{tag}] {line}", flush=True)
    return

    method = msg.get("method", "")
    params = msg.get("params", {})

    if method == "thread/started":
        thread = params.get("thread", {})
        tid = thread.get("id", "")[:8]
        model = thread.get("modelProvider", "")
        print(f"  [{tag}] codex thread {tid}" + (f" | {model}" if model else ""), flush=True)

    elif method == "turn/started":
        turn_id = params.get("turn", {}).get("id", "")[:8]
        print(f"  [{tag}] turn started {turn_id}", flush=True)

    elif method == "item/started":
        item = params.get("item", {})
        label = _codex_item_label(item)
        item_id = item.get("id") or params.get("itemId")
        if item_id:
            state.setdefault("item_labels", {})[item_id] = label
        if item.get("type") != "userMessage":
            print(f"  [{tag}] started {label}", flush=True)

    elif method == "item/completed":
        item = params.get("item", {})
        item_type = item.get("type", "")
        if item_type == "agentMessage":
            text = _compact_preview(item.get("text", ""), 160)
            if text:
                print(f"  [{tag}] assistant done: {text}", flush=True)
        elif item_type != "userMessage":
            label = _codex_item_label(item)
            status = item.get("status") or item.get("exitCode")
            suffix = f" ({status})" if status not in (None, "") else ""
            print(f"  [{tag}] completed {label}{suffix}", flush=True)

    elif method == "item/commandExecution/outputDelta":
        item_id = params.get("itemId", "")
        delta = params.get("delta", "")
        if not delta:
            return
        buffers = state.setdefault("command_output", {})
        buffers[item_id] = buffers.get(item_id, "") + delta
        now = time.time()
        last_key = f"command_output:{item_id}"
        last = state.get("last_print_at", {}).get(last_key, 0)
        if now - last >= 3:
            preview = _compact_preview(buffers[item_id], 180)
            if preview:
                print(f"  [{tag}] command output: {preview}", flush=True)
                state.setdefault("last_print_at", {})[last_key] = now

    elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded"):
        # Print summaries only, not raw reasoning text.
        delta = params.get("delta") or params.get("text") or ""
        if delta:
            print(f"  [{tag}] reasoning summary: {_compact_preview(delta, 180)}", flush=True)
        else:
            print(f"  [{tag}] reasoning summary updated", flush=True)

    elif method == "item/plan/delta":
        delta = params.get("delta", "")
        if delta:
            print(f"  [{tag}] plan: {_compact_preview(delta, 180)}", flush=True)

    elif method == "item/fileChange/patchUpdated":
        path = params.get("path") or params.get("filePath") or ""
        print(f"  [{tag}] patch updated" + (f": {path}" if path else ""), flush=True)

    elif method == "thread/tokenUsage/updated":
        usage = params.get("tokenUsage", {}).get("total", {})
        total = usage.get("totalTokens")
        if total is not None:
            print(
                f"  [{tag}] tokens total={total} input={usage.get('inputTokens')} output={usage.get('outputTokens')}",
                flush=True,
            )

    elif method == "turn/completed":
        turn = params.get("turn", {})
        status = turn.get("status", "completed")
        duration = turn.get("durationMs")
        suffix = f" in {duration / 1000:.1f}s" if duration is not None else ""
        print(f"  [{tag}] turn {status}{suffix}", flush=True)


def _run_streaming(cmd, input_text, tag, timeout=180, on_tools=None, progress_log_path=None):
    """Run claude CLI with stream-json, show events on terminal.
    on_tools(tools_list) is called for tool JSON found in intermediate assistant texts.
    Returns (result_text, rate_limit_msg_or_None, returncode, executed_tools)."""
    print(f"  [{tag}] launching: {' '.join(cmd[:6])}...", flush=True)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=PROJECT_ROOT,
    )

    if input_text:
        try:
            proc.stdin.write(input_text)
        except BrokenPipeError:
            print(f"  [{tag}] stdin broken pipe", flush=True)
    proc.stdin.close()

    result_text = ""
    rate_limit_msg = None
    result_error_subtype = ""  # set if result event has is_error=true
    result_error_msg = ""      # first entry from errors array
    all_texts = []  # fallback if no result event
    raw_lines = []  # collect all raw output for debugging
    executed_tools = []  # tool specs already executed mid-stream
    deadline = time.time() + timeout
    progress_state = {}

    while True:
        if time.time() > deadline:
            proc.kill()
            proc.wait()
            raise subprocess.TimeoutExpired(cmd, timeout)

        line = proc.stdout.readline()
        if not line:
            break

        raw_lines.append(line.rstrip())
        line = line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # Not JSON — could be plain text output
            print(f"  [{tag}] (raw) {line[:200]}", flush=True)
            all_texts.append(line)
            if _is_rate_limit(line):
                rate_limit_msg = line
            continue

        progress_events = claude_progress_events(event, progress_state)
        for progress in progress_events:
            write_progress_line(progress_log_path, progress)
        _display_stream_event(event, tag, progress_state, progress_events)

        etype = event.get("type", "")

        if etype == "result":
            result_text = event.get("result", "")
            if result_text and _is_rate_limit(result_text):
                rate_limit_msg = result_text
            # Track errors — errors array has the actual messages
            if event.get("is_error"):
                result_error_subtype = event.get("subtype", "")
                errors = event.get("errors", [])
                if errors:
                    result_error_msg = errors[0]
                    print(f"  [{tag}] error: {result_error_msg}", flush=True)

        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    txt = block.get("text", "").strip()
                    if txt:
                        all_texts.append(txt)
                        if _is_rate_limit(txt):
                            rate_limit_msg = txt

                        # Try to parse as tool JSON and execute mid-stream
                        if on_tools:
                            tools_data = parse_manager_output(txt)
                            if tools_data and "tools" in tools_data:
                                tools_list = tools_data["tools"]
                                succeeded = on_tools(tools_list)
                                if succeeded:
                                    executed_tools.extend(succeeded)

    stderr = proc.stderr.read()
    rc = proc.wait()

    if stderr:
        print(f"  [{tag}] stderr: {stderr.strip()[:300]}", flush=True)
        if _is_rate_limit(stderr):
            if not rate_limit_msg:
                rate_limit_msg = stderr.strip()

    if not result_text and not all_texts:
        print(f"  [{tag}] empty output (rc={rc}, {len(raw_lines)} lines)", flush=True)

    # If no result event, fall back to last assistant text
    if not result_text and all_texts:
        result_text = all_texts[-1]

    return result_text, rate_limit_msg, rc, executed_tools, stderr.strip() if stderr else "", result_error_subtype, result_error_msg


def claude_code(text, carbon_id, on_tools=None):
    """Invoke the Manager via claude CLI with streaming JSON.
    on_tools(tools_list) is called for mid-stream tool JSON in assistant texts.
    Returns (raw_text_output, rate_limit_message_or_None, executed_tools)."""
    session_id = _get_session_id(carbon_id)
    system_prompt = get_manager_prompt(carbon_id)
    prompt_file = _write_prompt_file(carbon_id, system_prompt)
    tag = f"manager:{carbon_id}"
    progress_log_path = _codex_manager_progress_file(_codex_manager_stream_file(carbon_id))

    # Stream with --resume
    cmd = [
        CLAUDE_CMD, "-p",
        "--resume", session_id,
        "--system-prompt-file", prompt_file,
        "--dangerously-skip-permissions",
        "--output-format=stream-json",
        "--verbose",
    ]

    try:
        result_text, rate_limit, rc, executed_tools, stderr_text, error_subtype, error_msg = _run_streaming(
            cmd,
            text,
            tag,
            on_tools=on_tools,
            progress_log_path=progress_log_path,
        )
        if rc == 0 and result_text.strip():
            return result_text.strip(), rate_limit, executed_tools
        # Session not found — check the exact error message
        if rc != 0 and "no" in error_msg.lower() and "found" in error_msg.lower() and session_id in error_msg:
            print(f"  [{tag}] {error_msg} — creating new session...", flush=True)
            new_sid = new_session(carbon_id)
            # Use --session-id to actually create the session (--resume only looks for existing)
            cmd_new = [
                CLAUDE_CMD, "-p",
                "--session-id", new_sid,
                "--system-prompt-file", prompt_file,
                "--dangerously-skip-permissions",
                "--output-format=stream-json",
                "--verbose",
            ]
            result_text, rate_limit, rc, executed_tools, stderr_text, error_subtype, error_msg = _run_streaming(
                cmd_new,
                text,
                tag,
                on_tools=on_tools,
                progress_log_path=progress_log_path,
            )
            if rc == 0 and result_text.strip():
                return result_text.strip(), rate_limit, executed_tools
            # If that also failed, give up gracefully
            from core.interface import reply_contact
            reply_contact("Manager session not found - send a message to start a new one.", carbon_id)
            return '{"tools": [{"tool": "do_nothing"}]}', None, []
    except subprocess.TimeoutExpired:
        from core.interface import reply_contact
        reply_contact("hold on, still working on this...", carbon_id)
        return TIMEOUT_MSG, None, []
    except Exception:
        pass

    # Fallback: plain text mode with current session
    print(f"  [{tag}] retrying without stream-json...", flush=True)
    session_id = _get_session_id(carbon_id)  # re-read in case new_session was called above
    cmd_fallback = [
        CLAUDE_CMD, "-p",
        "--resume", session_id,
        "--system-prompt-file", prompt_file,
        "--dangerously-skip-permissions",
    ]

    try:
        result = subprocess.run(
            cmd_fallback,
            input=text,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=PROJECT_ROOT,
        )
        output = result.stdout.strip()
        rl = output if (output and _is_rate_limit(output)) else None
        return output, rl, []
    except subprocess.TimeoutExpired:
        from core.interface import reply_contact
        reply_contact("hold on, still working on this...", carbon_id)
        return TIMEOUT_MSG, None, []
    except Exception as e:
        return f'{{"tools": [{{"tool": "reply", "message": "Manager error: {e}"}}, {{"tool": "do_nothing"}}]}}', None, []


class _CodexAppServer:
    """Minimal JSON-RPC client for `codex app-server` over stdio."""

    def __init__(self, tag, timeout=180, stream_log_path=None):
        self.tag = tag
        self.timeout = timeout
        self.stream_log_path = stream_log_path
        self.next_id = 1
        self.messages = queue.Queue()
        self.stderr_lines = []
        self.proc = subprocess.Popen(
            [
                CODEX_CMD, "app-server",
                "--listen", "stdio://",
                "--config", 'sandbox_mode="danger-full-access"',
                "--config", 'approval_policy="never"',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=PROJECT_ROOT,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self._write_stream_log(line)
            self.messages.put(("stdout", line))

    def _read_stderr(self):
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            self.stderr_lines.append(line)
            self._write_stream_log(json.dumps({"type": "codex.stderr", "message": line}))
            self.messages.put(("stderr", line))

    def _write_stream_log(self, line):
        if not self.stream_log_path or not line:
            return
        try:
            with open(self.stream_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def send(self, method, params=None, msg_id=None):
        if msg_id is None:
            msg_id = self.next_id
            self.next_id += 1
        payload = {"id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        return msg_id

    def respond(self, msg_id, result):
        self.proc.stdin.write(json.dumps({"id": msg_id, "result": result}) + "\n")
        self.proc.stdin.flush()

    def _handle_server_request(self, msg):
        method = msg.get("method", "")
        msg_id = msg.get("id")
        if msg_id is None:
            return False

        if method == "item/commandExecution/requestApproval":
            self.respond(msg_id, {"decision": "acceptForSession"})
            return True
        if method == "item/fileChange/requestApproval":
            self.respond(msg_id, {"decision": "acceptForSession"})
            return True
        if method == "item/tool/requestUserInput":
            self.respond(msg_id, {"canceled": True})
            return True
        if method == "mcpServer/elicitation/request":
            self.respond(msg_id, {"action": "cancel"})
            return True
        return False

    def request(self, method, params=None, timeout=None):
        req_id = self.send(method, params)
        deadline = time.time() + (timeout or self.timeout)

        while time.time() < deadline:
            try:
                source, line = self.messages.get(timeout=0.25)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(self._process_exit_message())
                continue

            if source == "stderr":
                continue

            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if self._handle_server_request(msg):
                continue
            if msg.get("id") == req_id:
                return msg

        raise subprocess.TimeoutExpired([CODEX_CMD, "app-server"], timeout or self.timeout)

    def _process_exit_message(self):
        detail = "\n".join(self.stderr_lines[-5:]).strip()
        return f"codex app-server exited with code {self.proc.returncode}" + (f": {detail}" if detail else "")


def _codex_thread_file(carbon_id):
    return _session_file(carbon_id, "codex")


def _codex_manager_stream_file(carbon_id):
    os.makedirs(MANAGER_STREAMS_DIR, exist_ok=True)
    safe_carbon_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", carbon_id)
    return os.path.join(MANAGER_STREAMS_DIR, f"{safe_carbon_id}-{int(time.time() * 1000)}.jsonl")


def _codex_manager_progress_file(stream_log_path):
    if stream_log_path.endswith(".jsonl"):
        return stream_log_path[:-6] + ".progress.jsonl"
    return stream_log_path + ".progress"


def _read_codex_thread_id(carbon_id):
    path = _codex_thread_file(carbon_id)
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read().strip()


def _write_codex_thread_id(carbon_id, thread_id):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with open(_codex_thread_file(carbon_id), "w") as f:
        f.write(thread_id)


def _codex_thread_params(system_prompt):
    return {
        "cwd": PROJECT_ROOT,
        "baseInstructions": system_prompt,
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
    }


def _codex_start_or_resume_thread(client, carbon_id, system_prompt):
    thread_id = _read_codex_thread_id(carbon_id)
    params = _codex_thread_params(system_prompt)

    if thread_id:
        resp = client.request("thread/resume", {**params, "threadId": thread_id}, timeout=60)
        if "result" in resp:
            return resp["result"]["thread"]["id"]
        print(f"  [manager:{carbon_id}] codex resume failed; creating new thread", flush=True)

    resp = client.request("thread/start", {**params, "ephemeral": False}, timeout=60)
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message", "codex thread/start failed"))
    thread_id = resp["result"]["thread"]["id"]
    _write_codex_thread_id(carbon_id, thread_id)
    return thread_id


def codex_app_server(text, carbon_id, on_tools=None):
    """Invoke the Manager through Codex app-server.
    Returns (raw_text_output, rate_limit_message_or_None, executed_tools)."""
    tag = f"manager:{carbon_id}"
    system_prompt = get_manager_prompt(carbon_id)
    client = None
    final_text = ""
    streamed_text = ""
    rate_limit_msg = None
    executed_tools = []
    seen_tool_keys = set()
    error_msg = ""
    last_preview_at = 0
    stream_log_path = _codex_manager_stream_file(carbon_id)
    progress_log_path = _codex_manager_progress_file(stream_log_path)
    stream_display_state = {}

    try:
        client = _CodexAppServer(tag, stream_log_path=stream_log_path)
        client.request("initialize", {
            "clientInfo": {"name": "silicon", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }, timeout=30)
        thread_id = _codex_start_or_resume_thread(client, carbon_id, system_prompt)

        turn_resp = client.request("turn/start", {
            "threadId": thread_id,
            "cwd": PROJECT_ROOT,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "input": [{"type": "text", "text": text}],
        }, timeout=60)
        if "error" in turn_resp:
            raise RuntimeError(turn_resp["error"].get("message", "codex turn/start failed"))

        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                source, line = client.messages.get(timeout=0.25)
            except queue.Empty:
                if client.proc.poll() is not None:
                    raise RuntimeError(client._process_exit_message())
                continue

            if source == "stderr":
                if _is_rate_limit(line):
                    rate_limit_msg = line
                continue

            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if client._handle_server_request(msg):
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})
            progress = codex_progress_event(msg, stream_display_state)
            write_progress_line(progress_log_path, progress)
            _display_codex_stream_event(msg, tag, stream_display_state)

            if method == "item/agentMessage/delta":
                delta = params.get("delta", "")
                if delta:
                    streamed_text += delta
                    now = time.time()
                    preview = streamed_text.strip()[:150].replace("\n", " ")
                    if preview and now - last_preview_at >= 5:
                        print(f"  [{tag}] {preview}", flush=True)
                        last_preview_at = now
                    if _is_rate_limit(streamed_text):
                        rate_limit_msg = streamed_text
                    if on_tools:
                        tools_data = parse_manager_output(streamed_text, debug=False)
                        if tools_data and "tools" in tools_data:
                            candidates = []
                            for tool in tools_data["tools"]:
                                key = json.dumps(tool, sort_keys=True)
                                if key not in seen_tool_keys:
                                    seen_tool_keys.add(key)
                                    candidates.append(tool)
                            succeeded = on_tools(candidates) if candidates else []
                            if succeeded:
                                executed_tools.extend(succeeded)

            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    final_text = item.get("text", "").strip() or streamed_text.strip()

            elif method == "error":
                err = params.get("error", {})
                error_msg = err.get("message", "")
                if _is_rate_limit(error_msg):
                    rate_limit_msg = error_msg

            elif method == "turn/completed":
                turn = params.get("turn", {})
                status = turn.get("status", "")
                if status == "failed" and not final_text:
                    turn_error = turn.get("error") or {}
                    error_msg = turn_error.get("message") or error_msg or "Codex turn failed"
                break
        else:
            raise subprocess.TimeoutExpired([CODEX_CMD, "app-server"], 180)

        output = final_text or streamed_text.strip()
        if output and _is_rate_limit(output):
            rate_limit_msg = output
        if output:
            print(f"  [{tag}] stream log: {stream_log_path}", flush=True)
            print(f"  [{tag}] progress log: {progress_log_path}", flush=True)
            return output, rate_limit_msg, executed_tools
        if error_msg:
            print(f"  [{tag}] stream log: {stream_log_path}", flush=True)
            print(f"  [{tag}] progress log: {progress_log_path}", flush=True)
            return f'{{"tools": [{{"tool": "reply", "message": "Manager error: {error_msg}"}}, {{"tool": "do_nothing"}}]}}', rate_limit_msg, executed_tools
        print(f"  [{tag}] stream log: {stream_log_path}", flush=True)
        print(f"  [{tag}] progress log: {progress_log_path}", flush=True)
        return "", rate_limit_msg, executed_tools

    except subprocess.TimeoutExpired:
        from core.interface import reply_contact
        reply_contact("hold on, still working on this...", carbon_id)
        return TIMEOUT_MSG, None, []
    except Exception as e:
        return f'{{"tools": [{{"tool": "reply", "message": "Manager error: {e}"}}, {{"tool": "do_nothing"}}]}}', None, []
    finally:
        if client:
            client.close()


def manager_code(text, carbon_id, on_tools=None):
    """Invoke the configured manager brain."""
    if get_brain() == "codex":
        return codex_app_server(text, carbon_id, on_tools=on_tools)
    return claude_code(text, carbon_id, on_tools=on_tools)


def parse_manager_output(output, debug=True):
    """Extract ALL tools JSON blocks from manager's text output.
    The manager may output one or more JSON blocks like: {"tools": [...]}
    Returns a merged {"tools": [...]} with all tools from all blocks, or None."""

    if debug:
        print(f"[DEBUG] Raw manager output:\n{output}\n", flush=True)

    if not output:
        return None

    # Strip markdown code blocks if present (```json ... ``` or ``` ... ```)
    cleaned = re.sub(r'```(?:json)?\s*', '', output)
    cleaned = re.sub(r'```', '', cleaned)

    all_tools = []

    # Find ALL JSON objects with "tools" key
    for text in [cleaned, output]:
        brace_depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == '{':
                if brace_depth == 0:
                    start = i
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth == 0 and start != -1:
                    candidate = text[start:i+1]
                    try:
                        parsed = json.loads(candidate)
                        if "tools" in parsed:
                            all_tools.extend(parsed["tools"])
                    except (json.JSONDecodeError, ValueError):
                        pass
                    start = -1

        # If we found tools in the cleaned version, don't re-scan the raw output
        if all_tools:
            break

    # Fallback: try the whole output as JSON
    if not all_tools:
        for text in [cleaned, output]:
            try:
                parsed = json.loads(text.strip())
                if "tools" in parsed:
                    all_tools.extend(parsed["tools"])
                    break
            except (json.JSONDecodeError, ValueError):
                pass

    if all_tools:
        return {"tools": all_tools}
    return None
