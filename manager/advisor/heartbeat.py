"""The manager heartbeat.

Every thirteen minutes, every manager is woken whether or not anyone spoke to
it. A Silicon that only ever runs when addressed can only ever react; the
heartbeat is the window in which it can notice that a worker has gone quiet,
that a carbon never replied, that a memory was never written down.

Each beat carries the manager's own active work with it, so the first thing it
sees is what it already has running — the same list `iwantto work --active`
would print.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from helpers.paths import DATA_ROOT, STATE_DIR
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


def _active_work_section(contact_id: str) -> str:
    """The manager's own open work, rendered as it would be on the CLI."""
    try:
        from diagnostics.iwantto.commands.work import _summary_line, active_works

        entries = active_works(contact_id)
    except Exception:
        return ""
    if not entries:
        return "\nYou have no active work right now."
    lines = "\n".join(
        _summary_line(work_id, work) for work_id, work in entries
    )
    return (
        "\nWork you have active right now "
        f"(`iwantto work --active --by {contact_id}`):\n{lines}"
    )


def contacts_due(now: float | None = None) -> list:
    """Every contact whose manager has not beaten in the last interval."""
    from interface import get_contacts

    now = _now() if now is None else now
    state = read_json(HEARTBEAT_STATE_FILE, _default_state())
    beats = state.get("contacts") or {}
    due = []
    for contact_id, contact in (get_contacts() or {}).items():
        if not isinstance(contact, dict):
            continue
        entry = beats.get(contact_id)
        last = float(entry.get("last_beat_at") or 0.0) if isinstance(entry, dict) else 0.0
        if not last:
            # A contact seen for the first time starts its clock now rather
            # than beating immediately, so adding a contact does not wake a
            # manager that has nothing yet to be woken about.
            _record(contact_id, now)
            continue
        if now - last >= INTERVAL_SECONDS:
            due.append(contact_id)
    return due


def _record(contact_id: str, now: float) -> None:
    def update(state):
        entry = state.setdefault("contacts", {}).setdefault(contact_id, {})
        entry["last_beat_at"] = now
        entry["last_beat_at_iso"] = _iso(now)
        entry["beats"] = int(entry.get("beats") or 0) + 1

    update_json(HEARTBEAT_STATE_FILE, _default_state(), update)


def build_context(contact_id: str) -> str:
    return (
        f"[HEARTBEAT]\n{BEAT_MESSAGE}\n"
        f"{_active_work_section(contact_id)}"
    )


def check_manager_heartbeats():
    """Event-loop handler. Returns {contact_id: context} for managers now due."""
    now = _now()
    due = contacts_due(now)
    if not due:
        return None
    contexts = {}
    for contact_id in due:
        contexts[contact_id] = build_context(contact_id)
        _record(contact_id, now)
    return contexts
