"""The Silicon heartbeat.

Every thirteen minutes the session is woken whether or not anyone spoke to it. A
Silicon that only ever runs when addressed can only ever react; the heartbeat is
the window in which it can notice that a worker has gone quiet, that a carbon
never replied, that a memory was never written down.

Each beat carries its own active work with it, so the first thing it sees is
what it already has running — the same list `iwantto work --active` would print.

There is one beat, not one per contact. A heartbeat belongs to nobody in
particular, so it carries no sender envelope and produces no progress in
anyone's room. If the Silicon decides a beat is worth telling someone about, it
says so explicitly with `iwantto send`.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.session import SILICON
from helpers.state import read_json, update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
HEARTBEAT_STATE_FILE = os.path.join(
    os.fspath(STATE_DIR), "heartbeats.json"
)

INTERVAL_SECONDS = 13 * 60
BEAT_MESSAGE = "congrats, your heart is beating, make it count!"


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_state() -> dict:
    return {"version": 1, "contacts": {}}


def _active_work_section() -> str:
    """The Silicon's own open work, rendered as it would be on the CLI."""
    try:
        from iwantto.commands.work import _summary_line, active_works

        entries = active_works(SILICON)
    except Exception:
        return ""
    if not entries:
        return "\nYou have no active work right now."
    lines = "\n".join(
        _summary_line(work_id, work) for work_id, work in entries
    )
    return f"\nWork you have active right now (`iwantto work --active`):\n{lines}"


def beat_due(now: float | None = None) -> bool:
    """Has the session gone an interval without beating?

    A first boot starts the clock rather than beating immediately, so a fresh
    instance is not woken about work it has never done.
    """
    now = _now() if now is None else now
    entry = (
        read_json(HEARTBEAT_STATE_FILE, _default_state()).get("contacts") or {}
    ).get(SILICON)
    last = float(entry.get("last_beat_at") or 0.0) if isinstance(entry, dict) else 0.0
    if not last:
        _record(now)
        return False
    return now - last >= INTERVAL_SECONDS


def _record(now: float) -> None:
    def update(state):
        entry = state.setdefault("contacts", {}).setdefault(SILICON, {})
        entry["last_beat_at"] = now
        entry["last_beat_at_iso"] = _iso(now)
        entry["beats"] = int(entry.get("beats") or 0) + 1

    update_json(HEARTBEAT_STATE_FILE, _default_state(), update)


def build_context() -> str:
    return f"[HEARTBEAT]\n{BEAT_MESSAGE}\n{_active_work_section()}"


def check_manager_heartbeats():
    """Event-loop handler. Returns ``{SILICON: context}`` when a beat is due."""
    now = _now()
    if not beat_due(now):
        return None
    context = build_context()
    _record(now)
    return {SILICON: context}
