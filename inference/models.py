"""The shapes that cross the inference boundary.

Turn requests carry live callbacks, so they are dataclasses — validating a
function reference buys nothing. Anything read back off disk or out of a
provider's stdout is untrusted and gets a pydantic model instead.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from pydantic import BaseModel

# stdin handling a worker launch needs from its provider.
STDIN_NONE = "none"          # the task is already on the command line
STDIN_TASK = "task"          # write the task, then close
STDIN_STREAM = "stream"      # keep the pipe open for mid-run messages


@dataclass
class TurnRequest:
    """One synchronous, streaming turn against a provider."""

    text: str
    contact_id: str
    system_prompt: str
    session_key: str = ""
    tag: str = ""
    # Called with a list of tool specs found mid-stream; returns those that ran.
    on_tools: Optional[Callable[[list[dict]], list[dict]]] = None
    # Called with (progress_event, display_line) as the provider works.
    on_progress: Optional[Callable[..., None]] = None
    diag_span: Any = None
    env: Optional[Mapping[str, str]] = None
    # Identity a mid-turn message is injected against; empty disables injection.
    inject_key: str = ""

    def resolved_session_key(self) -> str:
        return self.session_key or self.contact_id

    def resolved_tag(self) -> str:
        return self.tag or f"manager:{self.contact_id}"


@dataclass
class TurnResult:
    """What a provider produced for one turn."""

    output: str = ""
    rate_limit: Optional[str] = None
    executed_tools: list[dict] = field(default_factory=list)

    def as_tuple(self) -> tuple[str, Optional[str], list[dict]]:
        """The historical ``(output, rate_limit, executed_tools)`` triple."""
        return self.output, self.rate_limit, self.executed_tools


@dataclass
class WorkerLaunchSpec:
    """Everything a provider needs to build a detached worker command."""

    worker_id: str
    worker_type: str
    task: str
    system_prompt: str
    session_id: str = ""
    resume: bool = False
    streaming: bool = False
    cwd: str = ""
    # Where the provider may write scratch files (a system-prompt file, …).
    scratch_dir: str = ""
    model: str = ""


@dataclass
class WorkerCommand:
    """A launchable worker process description, provider-shaped."""

    argv: list[str]
    stdin: str = STDIN_NONE
    # Codex only learns its thread id from its own output, so the launcher must
    # wait for it before the run is recorded as active.
    captures_session_id: bool = False

    def popen_stdin(self):
        return subprocess.PIPE if self.stdin != STDIN_NONE else None


class WorkerOutcome(BaseModel):
    """A provider's own reading of a worker's output file."""

    result: str = ""
    completed: bool = False
    state: str = "failed"          # "completed" | "failed"
    session_id: str = ""
    auth_message: str = ""
