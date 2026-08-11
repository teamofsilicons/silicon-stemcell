"""The Advisor: one per manager, whose only job is to help it think.

    carbon
      |
    manager — advisor
      |
    3 workers

An advisor does no work and talks to nobody. It reads, it thinks, it tells the
manager what it is drifting away from. It runs on the same provider as the
manager and holds its own conversation, so advice accumulates across a working
session instead of starting cold every time.

Two things reach it. `iwantto get-advice "..."` is **synchronous** — the manager
blocks until the advice comes back, because advice you receive after you have
already acted is not advice. And a five-hourly heartbeat, whose output is
delivered to the manager the same way a worker's result is.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from helpers.paths import DATA_ROOT
from helpers.state import read_json, update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
ADVISOR_STATE_FILE = os.path.join(
    PROJECT_ROOT, "core", "interface_state", "advisors.json"
)

# A gap this long means the manager has moved on to something else; the advice
# that mattered an hour ago is context the advisor is better off without.
SESSION_GAP_SECONDS = 2 * 60 * 60
# And no session outlives a day, however busy it has been.
SESSION_MAX_AGE_SECONDS = 24 * 60 * 60
HEARTBEAT_INTERVAL_SECONDS = 5 * 60 * 60

HEARTBEAT_PROMPT = "[HEARTBEAT] Is your manager doing a good job, or should they change something?"

# The files that make an advisor, in the order it reads them.
ADVISOR_PROMPT_FILES = ("INDEX.md", "IWANTTO_CLI_REFERENCE.md", "ADVISOR.md")


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_state() -> dict:
    return {"version": 1, "advisors": {}}


def session_key(contact_id: str) -> str:
    """The advisor's own conversation, kept apart from the manager's."""
    return f"advisor__{contact_id}"


def _state_for(contact_id: str) -> dict:
    state = read_json(ADVISOR_STATE_FILE, _default_state())
    entry = (state.get("advisors") or {}).get(contact_id)
    return dict(entry) if isinstance(entry, dict) else {}


def build_prompt(contact_id: str) -> str:
    """INDEX.md, then IWANTTO_CLI_REFERENCE.md, then ADVISOR.md."""
    from prompts.DNA import _read_prompt

    parts = [_read_prompt(name) for name in ADVISOR_PROMPT_FILES]
    parts.append(
        "## Who you are advising\n"
        f"You are the advisor to the manager of `{contact_id}`. Your advice "
        "goes to that manager and nobody else. You do not message carbons, you "
        "do not do the work, and you do not act on the manager's behalf. Use "
        "`iwantto` only to read the context you need in order to advise well."
    )
    return "\n\n".join(part for part in parts if part)


def _should_rotate(entry: dict, now: float) -> str:
    """Why this advisor needs a fresh session, or an empty string if it does not."""
    if not entry:
        return "first advice for this manager"
    last = float(entry.get("last_invoked_at") or 0.0)
    started = float(entry.get("session_started_at") or 0.0)
    if last and now - last > SESSION_GAP_SECONDS:
        return "more than 2 hours since the last advice"
    if started and now - started > SESSION_MAX_AGE_SECONDS:
        return "the advisor session is over 24 hours old"
    return ""


def _rotate_session(contact_id: str, now: float) -> None:
    from manager import new_session

    new_session(session_key(contact_id))

    def update(state):
        entry = state.setdefault("advisors", {}).setdefault(contact_id, {})
        entry["session_started_at"] = now
        entry["session_started_at_iso"] = _iso(now)

    update_json(ADVISOR_STATE_FILE, _default_state(), update)


def _record_invocation(contact_id: str, now: float, *, heartbeat: bool) -> None:
    def update(state):
        entry = state.setdefault("advisors", {}).setdefault(contact_id, {})
        entry["last_invoked_at"] = now
        entry["last_invoked_at_iso"] = _iso(now)
        entry["invocations"] = int(entry.get("invocations") or 0) + 1
        if heartbeat:
            entry["last_heartbeat_at"] = now
            entry["last_heartbeat_at_iso"] = _iso(now)
        entry.setdefault("session_started_at", now)

    update_json(ADVISOR_STATE_FILE, _default_state(), update)


def ask(contact_id: str, question: str, *, heartbeat: bool = False) -> str:
    """Run the advisor and return its advice. Blocks until it answers."""
    from core.iwantto.actor import ADVISOR, issue_run_env, revoke_actor
    from manager import run_agent

    contact_id = str(contact_id or "")
    question = str(question or "").strip()
    if not contact_id:
        return "Error: an advisor belongs to a manager; no contact was given."
    if not question:
        return "Error: ask the advisor something."

    now = _now()
    reason = _should_rotate(_state_for(contact_id), now)
    if reason:
        _rotate_session(contact_id, now)

    # The environment is what lets the advisor run `iwantto` at all: it carries
    # the token that resolves it as this manager's advisor.
    from core.iwantto import journal

    token, env = issue_run_env(ADVISOR, contact_id, contact_id)
    started = time.monotonic()
    try:
        advice = run_agent(
            question,
            contact_id,
            session_key=session_key(contact_id),
            system_prompt=build_prompt(contact_id),
            tag=f"advisor:{contact_id}",
            env=env,
        )
    finally:
        revoke_actor(token)
    journal.record_run(
        ADVISOR,
        contact_id,
        contact_id,
        trigger=question[:200],
        seconds=time.monotonic() - started,
        ok=bool(advice),
        heartbeat=heartbeat,
        rotated=reason,
    )

    _record_invocation(contact_id, now, heartbeat=heartbeat)
    if not advice:
        return (
            "Your advisor could not be reached — every configured provider "
            "failed. Decide without it this time, and try again later."
        )
    return advice


def contacts_due_for_heartbeat(now: float | None = None) -> list:
    """Managers whose advisor has not been heard from in five hours."""
    from core.interface import get_contacts

    now = _now() if now is None else now
    state = read_json(ADVISOR_STATE_FILE, _default_state())
    advisors = state.get("advisors") or {}
    due = []
    for contact_id, contact in (get_contacts() or {}).items():
        if not isinstance(contact, dict):
            continue
        entry = advisors.get(contact_id)
        entry = entry if isinstance(entry, dict) else {}
        last = float(
            entry.get("last_heartbeat_at") or entry.get("last_invoked_at") or 0.0
        )
        if not last:
            # An advisor that has never run waits one full interval before its
            # first heartbeat, so a newly created contact is not immediately
            # advised about work that has not started.
            def seed(state_doc, key=contact_id, stamp=now):
                state_doc.setdefault("advisors", {}).setdefault(key, {})[
                    "last_heartbeat_at"
                ] = stamp

            update_json(ADVISOR_STATE_FILE, _default_state(), seed)
            continue
        if now - last >= HEARTBEAT_INTERVAL_SECONDS:
            due.append(contact_id)
    return due


def run_heartbeats() -> dict:
    """Advise every manager whose advisor is due. Returns {contact_id: context}.

    The advice is handed back as manager context, the same shape a worker
    result takes, so the manager reads it as a message from its advisor.
    """
    contexts = {}
    for contact_id in contacts_due_for_heartbeat():
        advice = ask(contact_id, HEARTBEAT_PROMPT, heartbeat=True)
        if not advice or advice.startswith("Error"):
            continue
        contexts[contact_id] = (
            "[Message from your Advisor]\n"
            f"{advice}"
        )
    return contexts
