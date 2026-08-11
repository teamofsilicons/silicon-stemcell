"""Thinking with Claude Code."""
from __future__ import annotations

import platform
import shutil
import subprocess

from interface.progress import claude_progress_events, provider_authentication_failed
from helpers.paths import CODE_ROOT
from inference.base import InferenceProvider
from inference.claude import output as claude_output
from inference.claude.stream import run_streaming
from inference.errors import (
    ProviderTimeoutError,
    error_tools,
    is_rate_limit,
    not_authenticated_tools,
)
from inference.limits import TURN_TIMEOUT
from inference.models import (
    STDIN_NONE,
    STDIN_STREAM,
    TurnRequest,
    TurnResult,
    WorkerCommand,
    WorkerLaunchSpec,
    WorkerOutcome,
)
from inference.sessions import prompt_file

PROJECT_ROOT = str(CODE_ROOT)


def _command() -> str:
    # On Windows, resolve the full path so the launch never needs shell=True
    # (which caps the command line at 8191 chars via cmd.exe).
    if platform.system() == "Windows":
        return shutil.which("claude") or shutil.which("claude.cmd") or "claude"
    return "claude"


CLAUDE_CMD = _command()


class ClaudeProvider(InferenceProvider):
    """The Claude Code CLI, driven over stream-json."""

    name = "claude"

    # -- conversations ----------------------------------------------------

    def new_session(self, session_key: str) -> str:
        return self.sessions.new_uuid(session_key)

    def session_id(self, session_key: str) -> str:
        return self.sessions.read_or_create_uuid(session_key)

    # -- synchronous turns ------------------------------------------------

    def _turn_command(self, session_flag: str, session_id: str, prompt_path: str):
        return [
            CLAUDE_CMD, "-p",
            session_flag, session_id,
            "--system-prompt-file", prompt_path,
            "--dangerously-skip-permissions",
            "--output-format=stream-json",
            "--verbose",
        ]

    def _stream(self, cmd, request: TurnRequest, tag: str):
        return run_streaming(
            cmd,
            request.text,
            tag,
            cwd=PROJECT_ROOT,
            on_tools=request.on_tools,
            on_progress=request.on_progress,
            diag_span=request.diag_span,
            env=request.env,
            streaming_input=True,
            inject_key=request.inject_key or request.contact_id,
        )

    def run_turn(self, request: TurnRequest) -> TurnResult:
        session_key = request.resolved_session_key()
        tag = request.resolved_tag()
        session_id = self.session_id(session_key)
        prompt_path = prompt_file(session_key, request.system_prompt)

        try:
            result = self._stream(
                self._turn_command("--resume", session_id, prompt_path),
                request,
                tag,
            )
            if result.succeeded():
                return TurnResult(
                    result.text.strip(), result.rate_limit, result.executed_tools
                )
            if result.authentication_failed():
                return TurnResult(
                    not_authenticated_tools(self.name),
                    None,
                    result.executed_tools,
                )
            if self._session_missing(result, session_id):
                return self._retry_with_new_session(
                    request, tag, prompt_path, result.executed_tools
                )
        except subprocess.TimeoutExpired as exc:
            self.new_session(session_key)
            raise ProviderTimeoutError(
                f"Claude manager turn timed out after {TURN_TIMEOUT:g} seconds"
            ) from exc
        except Exception:
            pass

        return self._plain_text_fallback(request, tag, prompt_path)

    def _session_missing(self, result, session_id: str) -> bool:
        """Claude reports a dropped session only in the error message text."""
        message = (result.error_message or "").lower()
        return bool(
            result.returncode != 0
            and "no" in message
            and "found" in message
            and session_id in result.error_message
        )

    def _retry_with_new_session(
        self, request: TurnRequest, tag: str, prompt_path: str, executed_tools
    ) -> TurnResult:
        session_key = request.resolved_session_key()
        print(
            f"  [{tag}] manager session missing — creating new session...",
            flush=True,
        )
        # `--session-id` actually creates the session; `--resume` only looks for
        # an existing one.
        new_id = self.new_session(session_key)
        result = self._stream(
            self._turn_command("--session-id", new_id, prompt_path), request, tag
        )
        if result.succeeded():
            return TurnResult(
                result.text.strip(), result.rate_limit, result.executed_tools
            )
        if result.authentication_failed():
            return TurnResult(
                not_authenticated_tools(self.name), None, result.executed_tools
            )
        return TurnResult(
            error_tools("Claude failed after creating a new session"),
            None,
            result.executed_tools,
        )

    def _plain_text_fallback(
        self, request: TurnRequest, tag: str, prompt_path: str
    ) -> TurnResult:
        """Last resort: one plain-text run on the session as it now stands."""
        session_key = request.resolved_session_key()
        print(f"  [{tag}] retrying without stream-json...", flush=True)
        session_id = self.session_id(session_key)
        cmd = [
            CLAUDE_CMD, "-p",
            "--resume", session_id,
            "--system-prompt-file", prompt_path,
            "--dangerously-skip-permissions",
        ]
        try:
            completed = subprocess.run(
                cmd,
                input=request.text,
                capture_output=True,
                text=True,
                timeout=TURN_TIMEOUT,
                cwd=PROJECT_ROOT,
                **({"env": request.env} if request.env is not None else {}),
            )
            text = completed.stdout.strip()
            if completed.returncode != 0 and provider_authentication_failed(
                completed.stdout, completed.stderr
            ):
                return TurnResult(not_authenticated_tools(self.name))
            return TurnResult(
                text, text if (text and is_rate_limit(text)) else None
            )
        except subprocess.TimeoutExpired as exc:
            self.new_session(session_key)
            raise ProviderTimeoutError(
                f"Claude manager fallback timed out after {TURN_TIMEOUT:g} seconds"
            ) from exc
        except Exception as exc:
            if provider_authentication_failed(exc):
                return TurnResult(not_authenticated_tools(self.name))
            return TurnResult(error_tools(exc))

    # -- detached runs ----------------------------------------------------

    def worker_command(self, spec: WorkerLaunchSpec) -> WorkerCommand:
        # Terminal workers append to Claude's own system prompt; the others
        # replace it outright.
        prompt_flag = (
            "--append-system-prompt"
            if spec.worker_type == "terminal"
            else "--system-prompt"
        )
        argv = [
            CLAUDE_CMD, "-p",
            "--resume" if spec.resume else "--session-id", spec.session_id,
            prompt_flag, spec.system_prompt,
            "--dangerously-skip-permissions",
            "--output-format=stream-json",
            "--verbose",
        ]
        if spec.model:
            argv.extend(["--model", spec.model])
        if spec.streaming:
            # The task arrives on stdin so the pipe stays open for messages the
            # manager sends while the worker is still working.
            argv.append("--input-format=stream-json")
        else:
            argv.append(spec.task)
        return WorkerCommand(
            argv=argv,
            stdin=STDIN_STREAM if spec.streaming else STDIN_NONE,
        )

    def read_output(self, raw: str) -> WorkerOutcome:
        return claude_output.read(raw)

    def progress_events(self, event: dict, state: dict) -> list[dict]:
        return claude_progress_events(event, state)
