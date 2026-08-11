"""Manager provider adapters: Claude Code and the Codex app-server.

Each ``brain`` is reached through a streaming subprocess that emits tool calls
and progress events as it works.  This module owns process lifecycle, per
contact session ids, timeout and rate-limit detection, redaction of provider
output, and parsing the manager's reply into a tools payload.
"""
import subprocess
import os
import json
import time
import uuid
import platform
import shutil
import queue
import threading
from contextlib import ExitStack, nullcontext

from prompts.DNA import get_manager_prompt
from core.codex_app_server import CodexAppServer as _SharedCodexAppServer
from helpers.paths import CODE_ROOT, DATA_ROOT
from core.iwantto import injection
from core.progress import (
    DONE,
    WRITING_FILE,
    claude_log_lines,
    claude_progress_events,
    codex_log_lines,
    codex_progress_event,
    contains_advertising_memory_reference,
    contains_private_manager_tool,
    diagnostic_error_summary,
    provider_authentication_failed,
    provider_not_authenticated_message,
    progress_is_error,
    progress_display_line,
    redact_diagnostic_text,
    usage_from_done_event,
)

IS_WINDOWS = platform.system() == "Windows"

PROJECT_ROOT = os.fspath(CODE_ROOT)
INSTANCE_ROOT = os.fspath(DATA_ROOT)
SESSIONS_DIR = os.path.join(INSTANCE_ROOT, "sessions")

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

# Manager turns have both an absolute ceiling and an inactivity ceiling. The
# latter catches app-server turns which report that thinking completed and then
# never emit agent text, a tool, an error, or turn/completed. Long work belongs
# in a worker instead of holding a contact's serialized manager queue.
MANAGER_TIMEOUT = max(
    60.0,
    float(os.environ.get("SILICON_MANAGER_TIMEOUT_SECONDS", str(30 * 60))),
)
MANAGER_INACTIVITY_TIMEOUT = max(
    30.0,
    min(
        MANAGER_TIMEOUT,
        float(os.environ.get("SILICON_MANAGER_INACTIVITY_SECONDS", "180")),
    ),
)

TIMEOUT_MSG = (
    "SYSTEM: The manager provider stopped responding before it produced a "
    "complete tool result. Delegate long-running work to a worker and finish "
    "this turn promptly."
)


class ManagerTimeoutError(TimeoutError):
    """A provider turn exceeded its absolute or inactivity deadline."""


SILICON_CONFIG_FILE = os.path.join(INSTANCE_ROOT, "silicon.json")


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


def get_brain_order():
    """Return the configured manager provider order.

    The first provider is the normal brain. Later entries are true fallbacks:
    they are tried only when the provider above fails before producing a usable
    manager response.
    """
    config = _read_silicon_config()
    raw = config.get("brain_order")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raw = [config.get("brain", "claude")]

    providers = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        provider = item.strip().lower()
        if provider == "chatgpt":
            provider = "codex"
        if provider in {"claude", "codex"} and provider not in seen:
            seen.add(provider)
            providers.append(provider)

    if providers:
        return providers
    return [get_brain()]


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


def _safe_manager_error_tools(value):
    """Build a Carbon-visible provider error without echoing private material."""
    detail = redact_diagnostic_text(value, limit=300)
    if detail in {
        "[private manager tool invocation omitted]",
        "[advertising memory content omitted]",
    }:
        detail = "provider call failed"
    detail = detail or "provider call failed"
    return json.dumps({
        "tools": [
            {"tool": "reply", "message": f"Manager error: {detail}"},
            {"tool": "do_nothing"},
        ]
    })


def _provider_not_authenticated_tools(provider):
    return json.dumps({
        "tools": [
            {
                "tool": "reply",
                "message": provider_not_authenticated_message(provider),
            },
            {"tool": "do_nothing"},
        ]
    })


def _provider_span(trace, provider):
    """Return a provider span context without allowing diagnostics failures out."""
    if trace is None:
        return nullcontext()
    try:
        span = trace.span("provider_call")
        span.set_meta(provider=provider)
        return span
    except Exception:
        return nullcontext()


def _attach_usage_to_span(span, progress):
    if span is None or not progress or progress.get("kind") != DONE:
        return
    try:
        span.set_tokens(**usage_from_done_event(progress))
        span.set_meta(
            provider_status=str(progress.get("status") or ""),
            provider_is_error=bool(progress_is_error(progress)),
        )
        if progress_is_error(progress):
            span.status = "error"
            summary = diagnostic_error_summary(progress)
            if summary:
                span.set_meta(error=summary)
    except Exception:
        pass


def _display_stream_event(event, tag, state=None, progress_events=None):
    """Print a stream-json event to terminal.

    Rendered from the raw event rather than the progress events, because
    ``progress_event`` blanks commands and their output at construction so they
    can never reach a Carbon. The operator running Silicon needs to see exactly
    what ran and what it printed, and the raw event never leaves this process.
    """
    progress_events = progress_events if progress_events is not None else claude_progress_events(event, state)
    log_lines = claude_log_lines(event, state if state is not None else {})
    if log_lines:
        for line in log_lines:
            print(f"  [{tag}] {line}", flush=True)
        return
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
                    # Do not stream manager prose/tool JSON into process logs.
                    # The centralized executor emits a redacted result later.
                    print(f"  [{tag}] assistant response", flush=True)
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


def _notify_progress(on_progress, progress):
    """Forward normalized provider progress without letting manager prose drive UI."""
    if not on_progress or not progress:
        return
    line = progress_display_line(progress)
    if not line:
        return
    try:
        on_progress(progress, line)
    except TypeError:
        try:
            on_progress(line)
        except Exception:
            pass
    except Exception:
        pass


def _display_codex_stream_event(msg, tag, state):
    """Print useful Codex app-server notifications as a live activity trace.

    Same reasoning as the Claude path: the raw message carries the real command
    and output, the progress event does not.
    """
    progress = codex_progress_event(msg, state)
    log_lines = codex_log_lines(msg, state)
    if log_lines:
        for line in log_lines:
            print(f"  [{tag}] {line}", flush=True)
        return
    line = progress_display_line(progress)
    if line:
        print(f"  [{tag}] {line}", flush=True)


# Streaming stdin is what makes a manager reachable mid-turn. Set
# SILICON_STREAMING_INPUT=0 to fall back to the single-shot behaviour.
STREAMING_INPUT = os.environ.get("SILICON_STREAMING_INPUT", "1") != "0"

INJECTED_PREFIX = (
    "[NEW MESSAGE from your carbon]\n\n"
)


def _stream_json_user(text):
    """One user message in the format `--input-format=stream-json` expects."""
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": str(text or "")}],
            },
        }
    ) + "\n"


class _ClaudeInjector:
    """Writes a new user message into a running `claude -p` session.

    The session stays reachable until its first `result`. After that the model
    has finished and anything newer belongs to the next run, so the injector
    refuses rather than writing into a turn that is already closing.
    """

    def __init__(self, proc, tag):
        self._proc = proc
        self._tag = tag
        self._lock = threading.Lock()
        self._open = True
        self.delivered = 0

    def submit(self, text):
        with self._lock:
            if not self._open:
                return False
            try:
                self._proc.stdin.write(_stream_json_user(text))
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._open = False
                return False
            self.delivered += 1
            print(f"  [{self._tag}] injected a new message mid-run", flush=True)
            return True

    def close(self):
        """Stop accepting and let the provider finish.

        Anything already written is in the pipe and will still be read, so a
        message accepted a moment before this is not lost.
        """
        with self._lock:
            if not self._open:
                return
            self._open = False
            try:
                self._proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass


class _CodexInjector:
    """Steers a live Codex turn with `turn/steer`.

    Uses ``send`` rather than ``request``: ``request`` drains the shared message
    queue, which would steal events from the loop reading the turn. The response
    comes back through that loop instead.
    """

    def __init__(self, client, thread_id, turn_id, tag):
        self._client = client
        self._thread_id = thread_id
        self._turn_id = turn_id
        self._tag = tag
        self._lock = threading.Lock()
        self._open = bool(thread_id and turn_id)
        self.delivered = 0
        self.request_ids = set()

    def submit(self, text):
        with self._lock:
            if not self._open:
                return False
            try:
                request_id = self._client.send(
                    "turn/steer",
                    {
                        "threadId": self._thread_id,
                        "expectedTurnId": self._turn_id,
                        "input": [{"type": "text", "text": str(text or "")}],
                    },
                )
            except (BrokenPipeError, OSError, ValueError):
                self._open = False
                return False
            self.request_ids.add(request_id)
            self.delivered += 1
            print(f"  [{self._tag}] steered the live turn with a new message", flush=True)
            return True

    def close(self):
        with self._lock:
            self._open = False


def _record_file_write(progress, env, tag):
    """Journal a file this run wrote, for the diagnosis store.

    The provider stream is the only place a Write/Edit is visible — the file is
    changed by the provider's own tools, not by Silicon. ``WRITING_FILE``
    progress events carry the path, so this is where "every file it writes"
    becomes knowable.
    """
    if not progress or progress.get("kind") != WRITING_FILE:
        return
    if progress.get("status") != "started":
        return
    path = progress.get("path")
    if not path:
        return
    try:
        from core.iwantto import journal
        from core.iwantto.actor import CONTACT_ENV, ID_ENV, KIND_ENV

        source = env if env is not None else os.environ
        journal.record_file_write(
            path,
            kind=str(source.get(KIND_ENV) or ""),
            actor_id=str(source.get(ID_ENV) or tag),
            contact_id=str(source.get(CONTACT_ENV) or ""),
            tool=str(progress.get("tool_name") or ""),
        )
    except Exception:
        pass


def _run_streaming(cmd, input_text, tag, timeout=MANAGER_TIMEOUT, on_tools=None,
                   on_progress=None, diag_span=None, env=None,
                   streaming_input=False, inject_key=""):
    """Run claude CLI with stream-json, show events on terminal.
    on_tools(tools_list) is called for tool JSON found in intermediate assistant texts.
    Returns (result_text, rate_limit_msg_or_None, returncode, executed_tools).

    ``env``, when given, replaces the inherited environment for this run only.
    It carries the run's `iwantto` identity token, which must not leak between
    managers running concurrently."""
    streaming_input = bool(streaming_input and STREAMING_INPUT)
    if streaming_input:
        cmd = [*cmd, "--input-format=stream-json"]
    print(f"  [{tag}] launching: {' '.join(cmd[:6])}...", flush=True)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=PROJECT_ROOT,
        **({"env": env} if env is not None else {}),
    )

    injector = _ClaudeInjector(proc, tag) if streaming_input else None
    if input_text:
        try:
            proc.stdin.write(
                _stream_json_user(input_text) if streaming_input else input_text
            )
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            print(f"  [{tag}] stdin broken pipe", flush=True)
    if injector is None:
        # Single-shot: the provider exits once it has answered.
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

    # Reachable while the turn runs: a message arriving now is written
    # straight into the live session instead of waiting for the next run.
    registration = (
        injection.accepting(injection.MANAGER, inject_key, injector.submit)
        if (injector is not None and inject_key)
        else nullcontext()
    )
    try:
        with registration:
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
                    print(f"  [{tag}] raw provider output omitted", flush=True)
                    all_texts.append(line)
                    if _is_rate_limit(line):
                        rate_limit_msg = line
                    continue

                progress_events = claude_progress_events(event, progress_state)
                for progress in progress_events:
                    _attach_usage_to_span(diag_span, progress)
                    _notify_progress(on_progress, progress)
                    _record_file_write(progress, env, tag)
                _display_stream_event(event, tag, progress_state, progress_events)

                etype = event.get("type", "")

                if etype == "result":
                    # The model has answered. Stop accepting injections and let it
                    # exit; anything already written is still in the pipe and read.
                    if injector is not None:
                        injector.close()
                    result_text = event.get("result", "")
                    if result_text and _is_rate_limit(result_text):
                        rate_limit_msg = result_text
                    # Track errors — errors array has the actual messages
                    if event.get("is_error"):
                        result_error_subtype = event.get("subtype", "")
                        errors = event.get("errors", [])
                        if errors:
                            result_error_msg = errors[0]
                            print(f"  [{tag}] provider error details omitted", flush=True)

                elif etype == "assistant":
                    if provider_authentication_failed(event.get("error")):
                        result_error_subtype = "authentication_failed"
                        result_error_msg = "authentication failed"
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            txt = block.get("text", "").strip()
                            if txt:
                                all_texts.append(txt)
                                if _is_rate_limit(txt):
                                    rate_limit_msg = txt

                                # Try to parse as tool JSON and execute mid-stream
                                if on_tools:
                                    tools_data = parse_manager_output(txt, debug=False)
                                    if tools_data and "tools" in tools_data:
                                        tools_list = tools_data["tools"]
                                        succeeded = on_tools(tools_list)
                                        if succeeded:
                                            executed_tools.extend(succeeded)

    finally:
        if injector is not None:
            injector.close()

    stderr = proc.stderr.read()
    rc = proc.wait()

    if stderr:
        print(f"  [{tag}] provider stderr omitted", flush=True)
        if _is_rate_limit(stderr):
            if not rate_limit_msg:
                rate_limit_msg = stderr.strip()

    if not result_text and not all_texts:
        print(f"  [{tag}] empty output (rc={rc}, {len(raw_lines)} lines)", flush=True)

    # If no result event, fall back to last assistant text
    if not result_text and all_texts:
        result_text = all_texts[-1]

    safe_stderr = ""
    if stderr:
        safe_stderr = (
            "[provider authentication failed]"
            if provider_authentication_failed(stderr)
            else "[provider stderr omitted]"
        )

    return (
        result_text,
        rate_limit_msg,
        rc,
        executed_tools,
        safe_stderr,
        result_error_subtype,
        result_error_msg,
    )


def claude_code(
    text,
    carbon_id,
    on_tools=None,
    on_progress=None,
    diag_span=None,
    session_key=None,
    system_prompt=None,
    tag=None,
    env=None,
):
    """Invoke the Manager via claude CLI with streaming JSON.
    on_tools(tools_list) is called for mid-stream tool JSON in assistant texts.
    Returns (raw_text_output, rate_limit_message_or_None, executed_tools).

    ``session_key`` and ``system_prompt`` let a non-manager agent — the advisor
    — reuse this provider path with its own conversation and instructions,
    while still resolving trust and paths against ``carbon_id``."""
    session_key = session_key or carbon_id
    session_id = _get_session_id(session_key)
    if system_prompt is None:
        system_prompt = get_manager_prompt(carbon_id)
    prompt_file = _write_prompt_file(session_key, system_prompt)
    tag = tag or f"manager:{carbon_id}"

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
            on_progress=on_progress,
            diag_span=diag_span,
            env=env,
            streaming_input=True,
            inject_key=carbon_id,
        )
        if rc == 0 and result_text.strip():
            return result_text.strip(), rate_limit, executed_tools
        if provider_authentication_failed(
            error_subtype,
            error_msg,
            stderr_text,
        ):
            return _provider_not_authenticated_tools("claude"), None, executed_tools
        # Session not found — check the exact error message
        if rc != 0 and "no" in error_msg.lower() and "found" in error_msg.lower() and session_id in error_msg:
            print(f"  [{tag}] manager session missing — creating new session...", flush=True)
            # MUST pass brain="claude": this is the claude path and needs a claude
            # UUID. Without it, new_session() defaults to the silicon's configured
            # brain — for a codex-brain silicon that returns the codex placeholder
            # string, which claude then rejects ("Invalid session ID"), surfacing
            # a spurious "Manager session not found".
            new_sid = new_session(session_key, brain="claude")
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
                on_progress=on_progress,
                diag_span=diag_span,
                env=env,
                streaming_input=True,
                inject_key=carbon_id,
            )
            if rc == 0 and result_text.strip():
                return result_text.strip(), rate_limit, executed_tools
            if provider_authentication_failed(
                error_subtype,
                error_msg,
                stderr_text,
            ):
                return (
                    _provider_not_authenticated_tools("claude"),
                    None,
                    executed_tools,
                )
            return (
                _safe_manager_error_tools(
                    "Claude failed after creating a new session"
                ),
                None,
                executed_tools,
            )
    except subprocess.TimeoutExpired as exc:
        new_session(session_key, brain="claude")
        raise ManagerTimeoutError(
            f"Claude manager turn timed out after {MANAGER_TIMEOUT:g} seconds"
        ) from exc
    except Exception:
        pass

    # Fallback: plain text mode with current session
    print(f"  [{tag}] retrying without stream-json...", flush=True)
    session_id = _get_session_id(session_key)  # re-read in case new_session was called above
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
            timeout=MANAGER_TIMEOUT,
            cwd=PROJECT_ROOT,
            **({"env": env} if env is not None else {}),
        )
        output = result.stdout.strip()
        if (
            result.returncode != 0
            and provider_authentication_failed(
                result.stdout,
                result.stderr,
            )
        ):
            return _provider_not_authenticated_tools("claude"), None, []
        rl = output if (output and _is_rate_limit(output)) else None
        return output, rl, []
    except subprocess.TimeoutExpired as exc:
        new_session(session_key, brain="claude")
        raise ManagerTimeoutError(
            f"Claude manager fallback timed out after "
            f"{MANAGER_TIMEOUT:g} seconds"
        ) from exc
    except Exception as e:
        if provider_authentication_failed(e):
            return _provider_not_authenticated_tools("claude"), None, []
        return _safe_manager_error_tools(e), None, []


def _redact_codex_agent_message(line, private_item_ids=None):
    """Remove assistant and advertising payloads from durable app-server traces."""
    private_item_ids = private_item_ids if private_item_ids is not None else set()
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError):
        return json.dumps(
            {"type": "provider.raw", "redacted": True},
            separators=(",", ":"),
        )
    if not isinstance(payload, dict):
        return json.dumps(
            {"type": "provider.raw", "redacted": True},
            separators=(",", ":"),
        )

    if str(payload.get("type") or "") in {
        "codex.stderr",
        "silicon.codex_app_error",
    }:
        return json.dumps(
            {
                "type": str(payload.get("type") or "provider.error"),
                "redacted": True,
            },
            separators=(",", ":"),
        )
    if "error" in payload and not payload.get("method"):
        safe_payload = {"error": {"redacted": True}}
        if "id" in payload:
            safe_payload["id"] = payload["id"]
        return json.dumps(safe_payload, separators=(",", ":"))
    if contains_private_manager_tool(json.dumps(payload, ensure_ascii=False)):
        return json.dumps(
            {"type": "provider.private", "redacted": True},
            separators=(",", ":"),
        )

    method = str(payload.get("method") or "")
    params = payload.get("params")
    item_id = ""
    item = None
    if isinstance(params, dict):
        item = params.get("item")
        if isinstance(item, dict):
            item_id = str(item.get("id") or params.get("itemId") or "")
        else:
            item_id = str(params.get("itemId") or "")
    command_execution = (
        method.startswith("item/commandExecution/")
        or (
            isinstance(item, dict)
            and str(item.get("type") or "") == "commandExecution"
        )
    )
    current_is_private = (
        command_execution
        or method == "error"
        or contains_private_manager_tool(
            json.dumps(params, ensure_ascii=False)
            if isinstance(params, (dict, list))
            else params
        )
        or contains_advertising_memory_reference(
            json.dumps(params, ensure_ascii=False)
            if isinstance(params, (dict, list))
            else params
        )
        or (item_id and item_id in private_item_ids)
    )
    if current_is_private and item_id:
        private_item_ids.add(item_id)
    if current_is_private:
        safe_params = {"redacted": True}
        if item_id:
            safe_params["itemId"] = item_id
        if isinstance(item, dict):
            safe_params["item"] = {
                key: item[key]
                for key in ("id", "type", "phase", "status")
                if key in item
            }
            safe_params["item"]["redacted"] = True
        return json.dumps(
            {"method": method, "params": safe_params},
            separators=(",", ":"),
        )

    if method.startswith("item/agentMessage/"):
        safe_params = {}
        if isinstance(params, dict) and params.get("itemId"):
            safe_params["itemId"] = params["itemId"]
        safe_params["redacted"] = True
        return json.dumps(
            {"method": method, "params": safe_params},
            separators=(",", ":"),
        )

    if method in {"item/started", "item/completed"} and isinstance(params, dict):
        item = params.get("item")
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            safe_item = {
                key: item[key]
                for key in ("id", "type", "phase", "status")
                if key in item
            }
            safe_item["redacted"] = True
            safe_params = {
                key: value
                for key, value in params.items()
                if key != "item"
            }
            safe_params["item"] = safe_item
            return json.dumps(
                {"method": method, "params": safe_params},
                separators=(",", ":"),
            )
    return line


class _CodexAppServer(_SharedCodexAppServer):
    """Manager presentation hooks around the shared app-server transport."""

    def __init__(self, tag, timeout=180, stream_log_path=None, env=None):
        self.tag = tag
        self.stream_log_path = stream_log_path
        self._private_stream_item_ids = set()
        super().__init__(
            PROJECT_ROOT,
            command=CODEX_CMD,
            timeout=timeout,
            env=env,
        )

    def _handle_stdout_line(self, line):
        self._write_stream_log(line)

    def _handle_stderr_line(self, line):
        self._write_stream_log(
            json.dumps({"type": "codex.stderr", "message": line})
        )

    def _write_stream_log(self, line):
        if not self.stream_log_path or not line:
            return
        try:
            private_item_ids = getattr(self, "_private_stream_item_ids", None)
            if private_item_ids is None:
                private_item_ids = set()
                self._private_stream_item_ids = private_item_ids
            line = _redact_codex_agent_message(
                line,
                private_item_ids,
            )
            with open(self.stream_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

def _codex_thread_file(carbon_id):
    return _session_file(carbon_id, "codex")


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
            result = resp["result"]
            return result["thread"]["id"], {
                "model": result.get("model", ""),
                "model_provider": result.get("modelProvider", ""),
            }
        print(f"  [manager:{carbon_id}] codex resume failed; creating new thread", flush=True)

    resp = client.request("thread/start", {**params, "ephemeral": False}, timeout=60)
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message", "codex thread/start failed"))
    thread_id = resp["result"]["thread"]["id"]
    _write_codex_thread_id(carbon_id, thread_id)
    return thread_id, {
        "model": resp["result"].get("model", ""),
        "model_provider": resp["result"].get("modelProvider", ""),
    }


def codex_app_server(
    text,
    carbon_id,
    on_tools=None,
    on_progress=None,
    diag_span=None,
    session_key=None,
    system_prompt=None,
    tag=None,
    env=None,
):
    """Invoke the Manager through Codex app-server.
    Returns (raw_text_output, rate_limit_message_or_None, executed_tools).

    ``session_key`` and ``system_prompt`` give the advisor its own Codex thread
    and instructions on the same transport."""
    session_key = session_key or carbon_id
    tag = tag or f"manager:{carbon_id}"
    if system_prompt is None:
        system_prompt = get_manager_prompt(carbon_id)
    # Holds the live-turn registration so a new message can steer this turn.
    stack = ExitStack()
    client = None
    final_text = ""
    streamed_text = ""
    rate_limit_msg = None
    executed_tools = []
    seen_tool_keys = set()
    error_msg = ""
    last_preview_at = 0
    stream_display_state = {}

    try:
        client = _CodexAppServer(tag, env=env)
        client.request("initialize", {
            "clientInfo": {"name": "silicon", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        }, timeout=30)
        thread_id, codex_context = _codex_start_or_resume_thread(client, session_key, system_prompt)
        codex_progress_event(
            {"type": "silicon.codex_context", **codex_context},
            stream_display_state,
        )

        turn_resp = client.request("turn/start", {
            "threadId": thread_id,
            "cwd": PROJECT_ROOT,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "input": [{"type": "text", "text": text}],
        }, timeout=60)
        if "error" in turn_resp:
            raise RuntimeError(turn_resp["error"].get("message", "codex turn/start failed"))

        # `turn/steer` requires the id of the turn it is steering and fails if
        # that turn is no longer the active one, so it is captured here.
        turn_id = str(
            ((turn_resp.get("result") or {}).get("turn") or {}).get("id") or ""
        )
        injector = _CodexInjector(client, thread_id, turn_id, tag)
        registration = (
            injection.accepting(injection.MANAGER, carbon_id, injector.submit)
            if turn_id
            else nullcontext()
        )
        stack.enter_context(registration)
        stack.callback(injector.close)

        started_at = time.time()
        deadline = started_at + MANAGER_TIMEOUT
        last_event_at = started_at
        while time.time() < deadline:
            now = time.time()
            if now - last_event_at >= MANAGER_INACTIVITY_TIMEOUT:
                raise subprocess.TimeoutExpired(
                    [CODEX_CMD, "app-server"],
                    MANAGER_INACTIVITY_TIMEOUT,
                )
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
            last_event_at = time.time()

            if client._handle_server_request(msg):
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})
            progress = codex_progress_event(msg, stream_display_state)
            _attach_usage_to_span(diag_span, progress)
            _notify_progress(on_progress, progress)
            _display_codex_stream_event(msg, tag, stream_display_state)

            if method == "item/agentMessage/delta":
                delta = params.get("delta", "")
                if delta:
                    streamed_text += delta
                    now = time.time()
                    if now - last_preview_at >= 5:
                        print(f"  [{tag}] assistant response streaming", flush=True)
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
            raise subprocess.TimeoutExpired([CODEX_CMD, "app-server"], MANAGER_TIMEOUT)

        output = final_text or streamed_text.strip()
        if output and _is_rate_limit(output):
            rate_limit_msg = output
        if output:
            return output, rate_limit_msg, executed_tools
        if error_msg:
            if provider_authentication_failed(error_msg):
                return (
                    _provider_not_authenticated_tools("codex"),
                    None,
                    executed_tools,
                )
            return _safe_manager_error_tools(error_msg), rate_limit_msg, executed_tools
        return "", rate_limit_msg, executed_tools

    except subprocess.TimeoutExpired as exc:
        # A timed-out persisted thread may still contain an unfinished turn.
        # Retrying that same thread can repeat the stall, so the next bounded
        # retry starts with a fresh thread.
        new_session(session_key, brain="codex")
        raise ManagerTimeoutError(
            "Codex manager turn stopped producing events before its deadline"
        ) from exc
    except Exception as e:
        if provider_authentication_failed(e):
            return _provider_not_authenticated_tools("codex"), None, []
        return _safe_manager_error_tools(e), None, []
    finally:
        stack.close()
        if client:
            client.close()


def manager_code(text, carbon_id, on_tools=None, on_progress=None, trace=None, env=None):
    """Invoke the configured manager brain.

    Fallback providers are only tried after the provider above returns a
    provider-level failure (empty output, timeout/rate-limit, or Manager error).
    Bad tool JSON from a successful provider is fed back to that same manager by
    the normal manager loop instead of silently switching brains.
    """
    last = None
    errors = []
    for provider in get_brain_order():
        try:
            with _provider_span(trace, provider) as diag_span:
                if provider == "codex":
                    result = codex_app_server(
                        text, carbon_id, on_tools=on_tools,
                        on_progress=on_progress, diag_span=diag_span,
                        env=env,
                    )
                else:
                    result = claude_code(
                        text, carbon_id, on_tools=on_tools,
                        on_progress=on_progress, diag_span=diag_span,
                        env=env,
                    )
        except ManagerTimeoutError:
            result = (
                TIMEOUT_MSG,
                None,
                [],
            )
        except Exception as exc:
            result = (
                _safe_manager_error_tools(exc),
                None,
                [],
            )

        last = result
        output, rate_limit, _executed_tools = result
        if not _manager_provider_failed(output, rate_limit):
            return result
        safe_failure = (
            redact_diagnostic_text(output or rate_limit, limit=200)
            or "provider failed"
        )
        errors.append(f"{provider}: {safe_failure}")

    if last is not None:
        if errors:
            print(f"  [manager:{carbon_id}] all configured brains failed: {' | '.join(errors)}", flush=True)
        return last
    return '{"tools": [{"tool": "do_nothing"}]}', None, []


def run_agent(
    text,
    carbon_id,
    *,
    session_key,
    system_prompt,
    tag,
    on_progress=None,
    env=None,
):
    """Run a non-manager agent on the manager's configured brain order.

    The advisor is the caller: same provider, same fallback rules, its own
    session and instructions. Returns the agent's final text, or an empty
    string if every configured provider failed.
    """
    errors = []
    for provider in get_brain_order():
        try:
            if provider == "codex":
                output, rate_limit, _tools = codex_app_server(
                    text,
                    carbon_id,
                    on_progress=on_progress,
                    session_key=session_key,
                    system_prompt=system_prompt,
                    tag=tag,
                    env=env,
                )
            else:
                output, rate_limit, _tools = claude_code(
                    text,
                    carbon_id,
                    on_progress=on_progress,
                    session_key=session_key,
                    system_prompt=system_prompt,
                    tag=tag,
                    env=env,
                )
        except ManagerTimeoutError:
            errors.append(f"{provider}: timed out")
            continue
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}")
            continue
        if output and output.strip() and not rate_limit:
            return output.strip()
        errors.append(f"{provider}: {'rate limited' if rate_limit else 'no output'}")
    if errors:
        print(f"  [{tag}] no provider produced a result: {' | '.join(errors)}", flush=True)
    return ""


def _manager_provider_failed(output, rate_limit):
    text = (output or "").strip()
    if not text:
        return True
    if text == TIMEOUT_MSG:
        return True
    # A complete tool invocation is usable even when its ordinary Markdown
    # happens to contain a phrase such as "rate limit". The one exception is
    # our own structured provider-error reply, which must still trigger the
    # configured fallback.
    parsed = parse_manager_output(text, debug=False)
    if parsed:
        for tool in parsed.get("tools", []):
            if not isinstance(tool, dict):
                continue
            message = str(tool.get("message") or "")
            if (
                tool.get("tool") == "reply"
                and (
                    "Manager error:" in message
                    or message in {
                        provider_not_authenticated_message("claude"),
                        provider_not_authenticated_message("codex"),
                    }
                )
            ):
                return True
        return False
    if rate_limit:
        return True
    if "Manager error:" in text:
        return True
    lowered = text.lower()
    if any(marker in lowered for marker in (
        "failed to authenticate",
        "authentication failed",
        "oauth session expired",
        "login required",
        "not logged in",
    )):
        return True
    return False


def parse_manager_output(output, debug=False):
    """Extract ALL tools JSON blocks from manager's text output.
    The manager may output one or more JSON blocks like: {"tools": [...]}
    Returns a merged {"tools": [...]} with all tools from all blocks, or None."""

    if debug:
        print(f"[DEBUG] Raw manager output:\n{output}\n", flush=True)

    if not output:
        return None

    all_tools = []
    text = str(output)
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            parsed, consumed = decoder.raw_decode(text[start:])
        except (json.JSONDecodeError, ValueError):
            index = start + 1
            continue
        index = start + max(consumed, 1)
        if not isinstance(parsed, dict):
            continue
        tools = parsed.get("tools")
        if not isinstance(tools, list):
            continue
        all_tools.extend(
            tool_spec
            for tool_spec in tools
            if isinstance(tool_spec, dict)
        )

    if all_tools:
        return {"tools": all_tools}
    return None
