"""The diagnosis store: everything that happens inside this Silicon.

Two things are recorded for every `iwantto` invocation.

The first is a durable append-only trail under ``diagnosis/<UTC-date>.jsonl`` —
who ran what, with which arguments, and what came back.  This is what you read
when you need to know why a Silicon did something last Tuesday.

The second is a compact per-run record in ``iwantto_runs.json``, keyed by the
actor token.  The manager loop uses it to answer one question it cannot answer
any other way: did this turn actually *do* anything?  Because commands now
execute mid-run rather than as an end-of-turn batch, the Stemcell never sees the
actions directly — it only sees that the run finished.
"""
from __future__ import annotations

import datetime
import json
import os
import time

from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.state import file_lock, read_json, update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
DIAGNOSIS_DIR = os.path.join(PROJECT_ROOT, "diagnosis")
RUNS_FILE = os.path.join(
    os.fspath(STATE_DIR), "iwantto_runs.json"
)

# Long-form arguments (a full message body, a work description) belong in the
# trail but must not make a single record unbounded.
MAX_FIELD_CHARS = 4000
MAX_RUNS = 512
# Runs are pruned by age rather than on completion, so a crashed manager cannot
# leave a record that blocks the file from ever shrinking.
RUN_TTL_SECONDS = 48 * 60 * 60


def _now() -> float:
    return time.time()


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _today_path() -> str:
    return os.path.join(DIAGNOSIS_DIR, f"{_utc_now():%Y-%m-%d}.jsonl")


def _clip(value):
    """Bound a value for storage without losing its shape."""
    if isinstance(value, str):
        if len(value) > MAX_FIELD_CHARS:
            return value[:MAX_FIELD_CHARS] + f"…(+{len(value) - MAX_FIELD_CHARS})"
        return value
    if isinstance(value, dict):
        return {str(key): _clip(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clip(item) for item in value[:50]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clip(str(value))


# The four kinds of thing the store must hold, so a whole session can be
# reconstructed: who ran, what they ran, what they said, and what they changed.
COMMAND = "command"
RUN = "run"
MESSAGE = "message"
FILE_WRITE = "file_write"


def _append(entry: dict) -> None:
    """Write one line to today's trail. Never raises into the caller.

    Diagnosis is evidence, not control flow. Losing a record must never fail
    the thing the Silicon was actually trying to do.
    """
    try:
        os.makedirs(DIAGNOSIS_DIR, mode=0o700, exist_ok=True)
        entry.setdefault("at", _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
        path = _today_path()
        with file_lock(path), open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def record(
    actor,
    command: str,
    *,
    args=None,
    result: str = "",
    ok: bool = True,
    extra=None,
) -> None:
    """Append one `iwantto` invocation — every command a Silicon runs."""
    entry = {
        "event": COMMAND,
        "kind": getattr(actor, "kind", ""),
        "actor_id": getattr(actor, "actor_id", ""),
        "contact_id": getattr(actor, "contact_id", ""),
        "command": command,
        "ok": bool(ok),
        "args": _clip(args or {}),
        "result": _clip(str(result or "")),
    }
    if extra:
        entry["extra"] = _clip(extra)
    _append(entry)


def record_run(
    kind: str,
    actor_id: str,
    contact_id: str,
    *,
    trigger: str = "",
    seconds: float | None = None,
    ok: bool = True,
    detail: str = "",
    **fields,
) -> None:
    """Append one agent invocation — a manager turn, an advisor turn, a worker run."""
    entry = {
        "event": RUN,
        "kind": str(kind or ""),
        "actor_id": str(actor_id or ""),
        "contact_id": str(contact_id or ""),
        "trigger": _clip(trigger),
        "ok": bool(ok),
        "detail": _clip(detail),
    }
    if seconds is not None:
        entry["seconds"] = round(float(seconds), 3)
    entry.update({key: _clip(value) for key, value in fields.items()})
    _append(entry)


def record_message(
    direction: str,
    contact_id: str,
    *,
    via: str = "",
    event_id: str = "",
    body: str = "",
    sender: str = "",
    ok: bool = True,
) -> None:
    """Append one message this Silicon sent or routed."""
    _append({
        "event": MESSAGE,
        "direction": str(direction or ""),
        "contact_id": str(contact_id or ""),
        "via": str(via or ""),
        "event_id": str(event_id or ""),
        "sender": str(sender or ""),
        "ok": bool(ok),
        "body": _clip(body),
    })


def record_file_write(
    path: str,
    *,
    kind: str = "",
    actor_id: str = "",
    contact_id: str = "",
    tool: str = "",
) -> None:
    """Append one file a manager, advisor, or worker wrote."""
    _append({
        "event": FILE_WRITE,
        "kind": str(kind or ""),
        "actor_id": str(actor_id or ""),
        "contact_id": str(contact_id or ""),
        "tool": str(tool or ""),
        "path": _clip(str(path or "")),
    })


def _default_runs() -> dict:
    return {"version": 1, "runs": {}}


def _prune(runs: dict, now: float) -> None:
    for token, entry in list(runs.items()):
        if not isinstance(entry, dict):
            runs.pop(token, None)
            continue
        started = float(entry.get("started_at") or 0.0)
        if now - started > RUN_TTL_SECONDS:
            runs.pop(token, None)
    if len(runs) <= MAX_RUNS:
        return
    ordered = sorted(
        runs.items(), key=lambda item: float(item[1].get("started_at") or 0.0)
    )
    for token, _entry in ordered[: len(runs) - MAX_RUNS]:
        runs.pop(token, None)


def note_invocation(token: str, command: str, *, ok: bool = True) -> None:
    """Count one command against the run that owns ``token``."""
    token = str(token or "")
    if not token:
        return
    now = _now()

    def update(state):
        runs = state.setdefault("runs", {})
        _prune(runs, now)
        entry = runs.setdefault(
            token, {"started_at": now, "commands": [], "count": 0}
        )
        entry["count"] = int(entry.get("count") or 0) + 1
        entry["last_at"] = now
        history = entry.setdefault("commands", [])
        history.append(command)
        # Only the shape of the turn matters here, not its full history.
        del history[:-50]
        if command == "do-nothing":
            entry["did_nothing"] = True
        elif ok:
            entry["acted"] = True

    try:
        update_json(RUNS_FILE, _default_runs(), update)
    except Exception:
        pass


def run_summary(token: str) -> dict:
    """What the run behind ``token`` did. Empty dict if it did nothing at all."""
    token = str(token or "")
    if not token:
        return {}
    state = read_json(RUNS_FILE, _default_runs())
    entry = (state.get("runs") or {}).get(token)
    return dict(entry) if isinstance(entry, dict) else {}


def clear_run(token: str) -> None:
    """Forget a finished run's counters."""
    token = str(token or "")
    if not token:
        return

    def update(state):
        state.setdefault("runs", {}).pop(token, None)

    try:
        update_json(RUNS_FILE, _default_runs(), update)
    except Exception:
        pass


def read_recent(limit: int = 100, *, days: int = 2) -> list:
    """Read the most recent invocations, newest last. For debugging the system."""
    entries: list = []
    today = _utc_now().date()
    for offset in range(max(1, days)):
        day = today - datetime.timedelta(days=offset)
        path = os.path.join(DIAGNOSIS_DIR, f"{day:%Y-%m-%d}.jsonl")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return entries[-limit:] if limit else entries
