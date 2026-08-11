"""One log file per agent, for as long as that agent exists.

The unit is the agent, not the day. A manager for `carbon-a` writes
``logs/manager/carbon-a.log`` on every run it ever has; the advisor that
Carbon spawns writes ``logs/advisor/carbon-a.log``; each worker writes
``logs/worker/<worker_id>.log``. Restarts append a ``SESSION`` line rather than
starting a new file, so reading one file end to end is reading that agent's
whole history — including where it stopped and came back, and which provider
was answering at the time.

Nothing here is ever rotated or deleted. Reconstructing what a Silicon did six
weeks ago is the entire point.

ponytail: unbounded growth is deliberate. If a file gets unmanageable, compress
old segments in place rather than truncating.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
from pathlib import Path

from helpers.paths import DATA_ROOT

LOGS_DIR = DATA_ROOT / "logs"
KINDS = ("silicon", "manager", "advisor", "worker")
MAX_FIELD = 2000

_write_lock = threading.Lock()
_instances: dict[tuple[str, str], "AgentLog"] = {}
_instances_lock = threading.Lock()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _safe(value: str) -> str:
    """A filename that survives whatever a contact id turns out to be."""
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in str(value or "unknown")
    ).strip("._-")
    return cleaned or "unknown"


def fmt(value) -> str:
    """One field, on one line, bounded."""
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    text = str(value).replace("\r", " ").replace("\n", "\\n").strip()
    if len(text) > MAX_FIELD:
        text = text[:MAX_FIELD] + f"…(+{len(text) - MAX_FIELD} chars)"
    return text


class AgentLog:
    """The durable record of one agent.

    Every method is best effort: a log write must never raise into the path of
    a Carbon's message.
    """

    def __init__(self, kind: str, agent_id: str = "") -> None:
        self.kind = kind if kind in KINDS else "silicon"
        self.agent_id = _safe(agent_id) if agent_id else ""

    @property
    def path(self) -> Path:
        if self.kind == "silicon" or not self.agent_id:
            return LOGS_DIR / "silicon.log"
        return LOGS_DIR / self.kind / f"{self.agent_id}.log"

    @property
    def inference_path(self) -> Path:
        name = f"{self.kind}-{self.agent_id}" if self.agent_id else self.kind
        return LOGS_DIR / "inference" / f"{name}.jsonl"

    # -- the record ------------------------------------------------------

    def event(self, category: str, message: str = "", **fields) -> None:
        """``[ts] CATEGORY | message | k=v | k=v``, appended."""
        line = f"[{_now():%Y-%m-%dT%H:%M:%S}Z] {category}"
        if message not in (None, ""):
            line += f" | {fmt(message)}"
        for key, value in fields.items():
            if value in (None, "", [], {}):
                continue
            line += f" | {key}={fmt(value)}"
        self._append(self.path, line + "\n")

    def session_start(self, session_id: str = "", provider: str = "", version: str = "") -> None:
        """Mark where a restart falls in this agent's history."""
        self.event(
            "SESSION",
            "started",
            session_id=session_id,
            provider=provider,
            version=version,
            pid=os.getpid(),
        )

    def inference(self, direction: str, **fields) -> None:
        """One JSONL record per call in or out of a model.

        This is the "what went in, what came out" trail: enough to replay a
        step and judge whether it was the right one.
        """
        record = {
            "at": f"{_now():%Y-%m-%dT%H:%M:%S}Z",
            "direction": direction,
            "kind": self.kind,
            "agent_id": self.agent_id,
        }
        record.update({key: value for key, value in fields.items() if value not in (None, "")})
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except Exception:
            return
        self._append(self.inference_path, line + "\n")

    def _append(self, path: Path, line: str) -> None:
        try:
            with _write_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception:
            pass


def agent_log(kind: str, agent_id: str = "") -> AgentLog:
    """The shared log for one agent. Same agent, same object, same file."""
    key = (kind, str(agent_id or ""))
    with _instances_lock:
        if key not in _instances:
            _instances[key] = AgentLog(kind, agent_id)
        return _instances[key]


def silicon_log() -> AgentLog:
    """The runtime's own log: boots, ticks, restarts, and what they cost."""
    return agent_log("silicon")


def runtime_log(message: str) -> None:
    """Say it on the terminal and keep it in the runtime's own log."""
    print(message, flush=True)
    silicon_log().event("RUNTIME", message)


def announce_session(session_id: str = "", provider: str = "", version: str = "") -> None:
    """Record a process start once, at the top of the runtime log."""
    silicon_log().session_start(session_id, provider, version)
