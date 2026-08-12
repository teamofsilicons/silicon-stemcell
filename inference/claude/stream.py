"""Running one ``claude -p`` process and reading its stream-json output."""
from __future__ import annotations

import json
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass, field

from iwantto import injection
from inference.claude.progress import claude_log_lines, claude_progress_events
from interface.progress import (
    progress_display_line,
    provider_authentication_failed,
)
from inference.claude.injector import ClaudeInjector
from inference.errors import is_rate_limit
from inference.limits import STREAMING_INPUT, TURN_TIMEOUT
from inference.parsing import parse_manager_output, stream_json_user
from inference.telemetry import attach_usage, notify_progress, record_file_write


def display_event(event, tag, state=None, progress_events=None) -> None:
    """Print a stream-json event to terminal.

    Rendered from the raw event rather than the progress events, because
    ``progress_event`` blanks commands and their output at construction so they
    can never reach a Carbon. The operator running Silicon needs to see exactly
    what ran and what it printed, and the raw event never leaves this process.
    """
    progress_events = (
        progress_events
        if progress_events is not None
        else claude_progress_events(event, state)
    )
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

    kind = event.get("type", "")

    if kind == "system" and event.get("subtype") == "init":
        model = event.get("model", "")
        sid = event.get("session_id", "")[:8]
        print(f"  [{tag}] session {sid} | {model}", flush=True)

    elif kind == "assistant":
        for block in event.get("message", {}).get("content", []):
            block_type = block.get("type", "")
            if block_type == "text":
                if block.get("text", "").strip():
                    # Do not stream manager prose/tool JSON into process logs.
                    # The centralized executor emits a redacted result later.
                    print(f"  [{tag}] assistant response", flush=True)
            elif block_type == "tool_use":
                print(f"  [{tag}] tool: {block.get('name', '?')}", flush=True)

    elif kind == "result":
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


@dataclass
class StreamResult:
    """One completed ``claude -p`` run, before it is judged."""

    text: str = ""
    rate_limit: str | None = None
    returncode: int = 0
    executed_tools: list = field(default_factory=list)
    stderr: str = ""
    error_subtype: str = ""
    error_message: str = ""

    def authentication_failed(self) -> bool:
        return bool(
            provider_authentication_failed(
                self.error_subtype,
                self.error_message,
                self.stderr,
            )
        )

    def succeeded(self) -> bool:
        return self.returncode == 0 and bool(self.text.strip())


def run_streaming(
    cmd,
    input_text,
    tag,
    *,
    cwd,
    timeout=TURN_TIMEOUT,
    on_tools=None,
    on_progress=None,
    diag_span=None,
    env=None,
    streaming_input=False,
    inject_key="",
) -> StreamResult:
    """Run the Claude CLI with stream-json and watch its events go by.

    ``on_tools(tools_list)`` is called for tool JSON found in intermediate
    assistant texts and returns the specs that actually ran.

    ``env``, when given, replaces the inherited environment for this run only.
    It carries the run's ``iwantto`` identity token, which must not leak between
    managers running concurrently.
    """
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
        cwd=cwd,
        **({"env": env} if env is not None else {}),
    )

    injector = ClaudeInjector(proc, tag) if streaming_input else None
    if input_text:
        try:
            proc.stdin.write(
                stream_json_user(input_text) if streaming_input else input_text
            )
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            print(f"  [{tag}] stdin broken pipe", flush=True)
    if injector is None:
        # Single-shot: the provider exits once it has answered.
        proc.stdin.close()

    result = StreamResult()
    all_texts = []   # fallback if no result event
    raw_lines = []   # collected only to report an empty run
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
                    if is_rate_limit(line):
                        result.rate_limit = line
                    continue

                progress_events = claude_progress_events(event, progress_state)
                for progress in progress_events:
                    attach_usage(diag_span, progress)
                    notify_progress(on_progress, progress)
                    record_file_write(progress, env, tag)
                display_event(event, tag, progress_state, progress_events)

                etype = event.get("type", "")

                if etype == "result":
                    # The model has answered. Stop accepting injections and let
                    # it exit; anything already written is still in the pipe.
                    if injector is not None:
                        injector.close()
                    result.text = event.get("result", "")
                    if result.text and is_rate_limit(result.text):
                        result.rate_limit = result.text
                    if event.get("is_error"):
                        result.error_subtype = event.get("subtype", "")
                        errors = event.get("errors", [])
                        if errors:
                            result.error_message = errors[0]
                            print(
                                f"  [{tag}] provider error details omitted",
                                flush=True,
                            )

                elif etype == "assistant":
                    if provider_authentication_failed(event.get("error")):
                        result.error_subtype = "authentication_failed"
                        result.error_message = "authentication failed"
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") != "text":
                            continue
                        txt = block.get("text", "").strip()
                        if not txt:
                            continue
                        all_texts.append(txt)
                        if is_rate_limit(txt):
                            result.rate_limit = txt

                        # Parse as tool JSON and execute mid-stream.
                        if on_tools:
                            tools_data = parse_manager_output(txt, debug=False)
                            if tools_data and "tools" in tools_data:
                                succeeded = on_tools(tools_data["tools"])
                                if succeeded:
                                    result.executed_tools.extend(succeeded)

    finally:
        if injector is not None:
            injector.close()

    stderr = proc.stderr.read()
    result.returncode = proc.wait()

    if stderr:
        print(f"  [{tag}] provider stderr omitted", flush=True)
        if is_rate_limit(stderr) and not result.rate_limit:
            result.rate_limit = stderr.strip()

    if not result.text and not all_texts:
        print(
            f"  [{tag}] empty output (rc={result.returncode}, "
            f"{len(raw_lines)} lines)",
            flush=True,
        )

    # If no result event, fall back to the last assistant text.
    if not result.text and all_texts:
        result.text = all_texts[-1]

    if stderr:
        result.stderr = (
            "[provider authentication failed]"
            if provider_authentication_failed(stderr)
            else "[provider stderr omitted]"
        )

    return result
