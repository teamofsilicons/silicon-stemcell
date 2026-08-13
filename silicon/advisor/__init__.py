"""The Advisor, whose only job is to help the Silicon think.

    carbons and silicons
              |
          silicon — advisor
              |
          3 workers

One advisor, for the one session. It does no work and talks to nobody. It
reads, it thinks, it says what the Silicon is drifting away from. It runs on the
same provider and holds its own conversation, so advice accumulates across a
working session instead of starting cold every time.

Two things reach it. `iwantto get-advice "..."` is **synchronous** — the caller
blocks until the advice comes back, because advice you receive after you have
already acted is not advice. And a five-hourly heartbeat, whose output is
delivered the same way a worker's result is.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.session import SILICON
from helpers.state import read_json, update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
ADVISOR_STATE_FILE = os.path.join(
    os.fspath(STATE_DIR), "advisors.json"
)

# A gap this long means the manager has moved on to something else; the advice
# that mattered an hour ago is context the advisor is better off without.
SESSION_GAP_SECONDS = 2 * 60 * 60
# And no session outlives a day, however busy it has been.
SESSION_MAX_AGE_SECONDS = 24 * 60 * 60
HEARTBEAT_INTERVAL_SECONDS = 5 * 60 * 60

HEARTBEAT_PROMPT = "[HEARTBEAT] Is the Silicon you advise doing a good job, or should it change something?"

# The files that make an advisor, in the order it reads them.
ADVISOR_PROMPT_FILES = ("INDEX.md", "IWANTTO_CLI_REFERENCE.md", "ADVISOR.md")


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_state() -> dict:
    return {"version": 1, "advisors": {}}


def session_key(contact_id: str = SILICON) -> str:
    """The advisor's own conversation, kept apart from the session it advises."""
    return f"advisor__{contact_id}"


def _state_for(contact_id: str) -> dict:
    state = read_json(ADVISOR_STATE_FILE, _default_state())
    entry = (state.get("advisors") or {}).get(contact_id)
    return dict(entry) if isinstance(entry, dict) else {}


def build_prompt(contact_id: str = SILICON) -> str:
    """INDEX.md, then IWANTTO_CLI_REFERENCE.md, then ADVISOR.md."""
    from prompts.loader import _read_prompt

    parts = [_read_prompt(name) for name in ADVISOR_PROMPT_FILES]
    parts.append(
        "## Who you are advising\n"
        "You are the advisor to this Silicon — the one session that answers "
        "every Carbon and every Silicon that talks to it. Your advice goes to "
        "it and nobody else. You do not message carbons, you do not do the "
        "work, and you do not act on its behalf. Use `iwantto` only to read the "
        "context you need in order to advise well."
    )
    return "\n\n".join(part for part in parts if part)


def _should_rotate(entry: dict, now: float) -> str:
    """Why the advisor needs a fresh session, or an empty string if it does not."""
    if not entry:
        return "first advice given"
    last = float(entry.get("last_invoked_at") or 0.0)
    started = float(entry.get("session_started_at") or 0.0)
    if last and now - last > SESSION_GAP_SECONDS:
        return "more than 2 hours since the last advice"
    if started and now - started > SESSION_MAX_AGE_SECONDS:
        return "the advisor session is over 24 hours old"
    return ""


def _rotate_session(contact_id: str, now: float) -> None:
    from silicon import new_session

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


def ask(contact_id: str = SILICON, question: str = "", *, heartbeat: bool = False) -> str:
    """Run the advisor and return its advice. Blocks until it answers."""
    from iwantto.actor import ADVISOR, issue_run_env, revoke_actor
    from silicon import run_agent

    contact_id = str(contact_id or SILICON)
    question = str(question or "").strip()
    if not question:
        return "Error: ask the advisor something."

    now = _now()
    reason = _should_rotate(_state_for(contact_id), now)
    if reason:
        _rotate_session(contact_id, now)

    # The environment is what lets the advisor run `iwantto` at all: it carries
    # the token that resolves it as this manager's advisor.
    from diagnostics import journal

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


def heartbeat_due(now: float | None = None) -> bool:
    """Has the advisor gone five hours without being heard from?"""
    now = _now() if now is None else now
    state = read_json(ADVISOR_STATE_FILE, _default_state())
    entry = (state.get("advisors") or {}).get(SILICON)
    entry = entry if isinstance(entry, dict) else {}
    last = float(
        entry.get("last_heartbeat_at") or entry.get("last_invoked_at") or 0.0
    )
    if not last:
        # An advisor that has never run waits one full interval before its
        # first heartbeat, so a fresh instance is not immediately advised
        # about work that has not started.
        def seed(state_doc):
            state_doc.setdefault("advisors", {}).setdefault(SILICON, {})[
                "last_heartbeat_at"
            ] = now

        update_json(ADVISOR_STATE_FILE, _default_state(), seed)
        return False
    return now - last >= HEARTBEAT_INTERVAL_SECONDS


def run_heartbeats() -> dict:
    """Advise the Silicon if its advisor is due. Returns ``{SILICON: context}``.

    The advice is handed back as session context, the same shape a worker
    result takes, so it reads as a message from the advisor.
    """
    if not heartbeat_due():
        return {}
    advice = ask(SILICON, HEARTBEAT_PROMPT, heartbeat=True)
    if not advice or advice.startswith("Error"):
        return {}
    return {SILICON: f"[Message from your Advisor]\n{advice}"}
