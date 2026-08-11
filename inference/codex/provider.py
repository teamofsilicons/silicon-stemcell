"""Thinking with the Codex app-server."""
from __future__ import annotations

import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack, nullcontext

from diagnostics.iwantto import injection
from inference.codex.progress import codex_log_lines, codex_progress_event
from interface.progress import (
    progress_display_line,
    provider_authentication_failed,
)
from helpers.paths import CODE_ROOT
from inference.base import InferenceProvider
from inference.codex import output as codex_output
from inference.codex.app_server import TracedAppServer
from inference.codex.injector import CodexInjector
from inference.errors import (
    ProviderTimeoutError,
    error_tools,
    is_rate_limit,
    not_authenticated_tools,
)
from inference.limits import INACTIVITY_TIMEOUT, TURN_TIMEOUT
from inference.models import (
    STDIN_TASK,
    TurnRequest,
    TurnResult,
    WorkerCommand,
    WorkerLaunchSpec,
    WorkerOutcome,
)
from inference.parsing import parse_manager_output
from inference.telemetry import attach_usage, notify_progress

PROJECT_ROOT = str(CODE_ROOT)
APP_WORKER = os.path.join(PROJECT_ROOT, "worker", "codex_app_worker.py")


def _command() -> str:
    if platform.system() == "Windows":
        return shutil.which("codex") or shutil.which("codex.cmd") or "codex"
    return "codex"


CODEX_CMD = _command()


def display_event(msg, tag, state) -> None:
    """Print an app-server notification as a live activity trace.

    Rendered from the raw message rather than the progress event: the raw one
    carries the real command and output, and never leaves this process.
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


class CodexProvider(InferenceProvider):
    """The Codex app-server, driven over JSON-RPC on stdio."""

    name = "codex"
    mints_own_session_id = True

    # -- conversations ----------------------------------------------------

    def new_session(self, session_key: str) -> str:
        # Codex thread ids are minted by the server, so resetting means
        # forgetting the current one and letting the next turn start a thread.
        self.sessions.clear(session_key)
        return "new codex thread will be created on next turn"

    def _thread_params(self, system_prompt: str) -> dict:
        return {
            "cwd": PROJECT_ROOT,
            "baseInstructions": system_prompt,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }

    def start_or_resume_thread(self, client, session_key: str, system_prompt: str):
        """Return ``(thread_id, context)``, resuming the stored thread if it lives."""
        thread_id = self.sessions.read(session_key)
        params = self._thread_params(system_prompt)

        if thread_id:
            response = client.request(
                "thread/resume", {**params, "threadId": thread_id}, timeout=60
            )
            if "result" in response:
                result = response["result"]
                return result["thread"]["id"], {
                    "model": result.get("model", ""),
                    "model_provider": result.get("modelProvider", ""),
                }
            print(
                f"  [manager:{session_key}] codex resume failed; "
                "creating new thread",
                flush=True,
            )

        response = client.request(
            "thread/start", {**params, "ephemeral": False}, timeout=60
        )
        if "error" in response:
            raise RuntimeError(
                response["error"].get("message", "codex thread/start failed")
            )
        thread_id = response["result"]["thread"]["id"]
        self.sessions.write(session_key, thread_id)
        return thread_id, {
            "model": response["result"].get("model", ""),
            "model_provider": response["result"].get("modelProvider", ""),
        }

    # -- synchronous turns ------------------------------------------------

    def run_turn(self, request: TurnRequest) -> TurnResult:
        session_key = request.resolved_session_key()
        tag = request.resolved_tag()
        # Holds the live-turn registration so a new message can steer this turn.
        stack = ExitStack()
        client = None
        turn = _Turn(tag, request)

        try:
            client = TracedAppServer(
                tag, PROJECT_ROOT, command=CODEX_CMD, env=request.env
            )
            client.request("initialize", {
                "clientInfo": {"name": "silicon", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            }, timeout=30)
            thread_id, context = self.start_or_resume_thread(
                client, session_key, request.system_prompt
            )
            codex_progress_event(
                {"type": "silicon.codex_context", **context}, turn.display_state
            )

            response = client.request("turn/start", {
                "threadId": thread_id,
                "cwd": PROJECT_ROOT,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
                "input": [{"type": "text", "text": request.text}],
            }, timeout=60)
            if "error" in response:
                raise RuntimeError(
                    response["error"].get("message", "codex turn/start failed")
                )

            # `turn/steer` needs the id of the turn it is steering and fails if
            # that turn is no longer the active one, so it is captured here.
            turn_id = str(
                ((response.get("result") or {}).get("turn") or {}).get("id") or ""
            )
            injector = CodexInjector(client, thread_id, turn_id, tag)
            stack.enter_context(
                injection.accepting(
                    injection.MANAGER,
                    request.inject_key or request.contact_id,
                    injector.submit,
                )
                if turn_id
                else nullcontext()
            )
            stack.callback(injector.close)

            turn.consume(client)
            return turn.result()

        except subprocess.TimeoutExpired as exc:
            # A timed-out persisted thread may still contain an unfinished turn.
            # Retrying that same thread can repeat the stall, so the next
            # bounded retry starts with a fresh thread.
            self.new_session(session_key)
            raise ProviderTimeoutError(
                "Codex manager turn stopped producing events before its deadline"
            ) from exc
        except Exception as exc:
            if provider_authentication_failed(exc):
                return TurnResult(not_authenticated_tools(self.name))
            return TurnResult(error_tools(exc))
        finally:
            stack.close()
            if client:
                client.close()

    # -- detached runs ----------------------------------------------------

    def worker_command(self, spec: WorkerLaunchSpec) -> WorkerCommand:
        # The app-server reads its instructions from a file, not from argv.
        scratch = spec.scratch_dir or PROJECT_ROOT
        os.makedirs(scratch, exist_ok=True)
        prompt_path = os.path.join(scratch, f"_{spec.worker_id}_codex_prompt.md")
        with open(prompt_path, "w", encoding="utf-8") as handle:
            handle.write(spec.system_prompt)

        argv = [
            sys.executable,
            APP_WORKER,
            "--cwd", spec.cwd or PROJECT_ROOT,
            "--system-prompt-file", prompt_path,
        ]
        if spec.session_id:
            argv.extend(["--thread-id", spec.session_id])
        return WorkerCommand(
            argv=argv,
            stdin=STDIN_TASK,
            # Codex only names its thread in its own output, so a fresh run is
            # not recorded as active until that id has been read back.
            captures_session_id=not spec.resume,
        )

    def read_output(self, raw: str) -> WorkerOutcome:
        return codex_output.read(raw)

    def progress_events(self, event: dict, state: dict) -> list[dict]:
        progress = codex_progress_event(event, state)
        return [progress] if progress else []

    def log_lines(self, event: dict, state: dict) -> list[str]:
        return codex_log_lines(event, state)


class _Turn:
    """The event loop of one app-server turn, and what it accumulated."""

    def __init__(self, tag: str, request: TurnRequest) -> None:
        self.tag = tag
        self.request = request
        self.display_state: dict = {}
        self.final_text = ""
        self.streamed_text = ""
        self.rate_limit = None
        self.executed_tools: list = []
        self.error_message = ""
        self._seen_tool_keys: set[str] = set()
        self._last_preview_at = 0.0

    def consume(self, client) -> None:
        started_at = time.time()
        deadline = started_at + TURN_TIMEOUT
        last_event_at = started_at
        while time.time() < deadline:
            if time.time() - last_event_at >= INACTIVITY_TIMEOUT:
                raise subprocess.TimeoutExpired(
                    [CODEX_CMD, "app-server"], INACTIVITY_TIMEOUT
                )
            try:
                source, line = client.messages.get(timeout=0.25)
            except queue.Empty:
                if client.proc.poll() is not None:
                    raise RuntimeError(client.process_exit_message())
                continue

            if source == "stderr":
                if is_rate_limit(line):
                    self.rate_limit = line
                continue

            try:
                message = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            last_event_at = time.time()

            if client.handle_server_request(message):
                continue
            if self._handle(message):
                return
        raise subprocess.TimeoutExpired([CODEX_CMD, "app-server"], TURN_TIMEOUT)

    def _handle(self, message) -> bool:
        """Process one notification. Returns True when the turn is over."""
        method = message.get("method", "")
        params = message.get("params", {})
        progress = codex_progress_event(message, self.display_state)
        attach_usage(self.request.diag_span, progress)
        notify_progress(self.request.on_progress, progress)
        display_event(message, self.tag, self.display_state)

        if method == "item/agentMessage/delta":
            self._on_delta(params.get("delta", ""))
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                self.final_text = (
                    item.get("text", "").strip() or self.streamed_text.strip()
                )
        elif method == "error":
            self.error_message = (params.get("error", {}) or {}).get("message", "")
            if is_rate_limit(self.error_message):
                self.rate_limit = self.error_message
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if turn.get("status") == "failed" and not self.final_text:
                turn_error = turn.get("error") or {}
                self.error_message = (
                    turn_error.get("message")
                    or self.error_message
                    or "Codex turn failed"
                )
            return True
        return False

    def _on_delta(self, delta: str) -> None:
        if not delta:
            return
        self.streamed_text += delta
        now = time.time()
        if now - self._last_preview_at >= 5:
            print(f"  [{self.tag}] assistant response streaming", flush=True)
            self._last_preview_at = now
        if is_rate_limit(self.streamed_text):
            self.rate_limit = self.streamed_text
        if not self.request.on_tools:
            return
        tools_data = parse_manager_output(self.streamed_text, debug=False)
        if not (tools_data and "tools" in tools_data):
            return
        candidates = []
        for tool in tools_data["tools"]:
            key = json.dumps(tool, sort_keys=True)
            if key not in self._seen_tool_keys:
                self._seen_tool_keys.add(key)
                candidates.append(tool)
        succeeded = self.request.on_tools(candidates) if candidates else []
        if succeeded:
            self.executed_tools.extend(succeeded)

    def result(self) -> TurnResult:
        output = self.final_text or self.streamed_text.strip()
        if output and is_rate_limit(output):
            self.rate_limit = output
        if output:
            return TurnResult(output, self.rate_limit, self.executed_tools)
        if self.error_message:
            if provider_authentication_failed(self.error_message):
                return TurnResult(
                    not_authenticated_tools("codex"), None, self.executed_tools
                )
            return TurnResult(
                error_tools(self.error_message),
                self.rate_limit,
                self.executed_tools,
            )
        return TurnResult("", self.rate_limit, self.executed_tools)
