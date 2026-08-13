"""Auxiliary commands: `start-new-session`, `do-nothing`, `restart-silicon`."""
from __future__ import annotations

import json
import os
import time

from helpers.paths import DATA_ROOT

PROJECT_ROOT = os.fspath(DATA_ROOT)
# The Stemcell owns process lifecycle. A command running in a child process
# cannot re-exec its parent, so it leaves a request the event loop picks up.
RESTART_REQUEST_FILE = os.path.join(PROJECT_ROOT, ".restart_requested")


def _error(message):
    from iwantto.cli import CommandError

    return CommandError(message)


def cmd_start_new_session(args, actor) -> str:
    """Drop the current conversation and open a fresh one.

    The first message carries context across the boundary — it is the one thing
    the new session gets to inherit, so it should say what was being discussed
    or what still needs doing.
    """
    from silicon import new_session

    if actor.is_worker:
        raise _error("Workers do not hold a session. Finish your task instead.")

    first_message = str(args.first_message or "").strip()
    session_id = new_session(actor.contact_id)
    if not first_message:
        return (
            f"New session started ({session_id}). Nothing was carried over — "
            "make sure your memory was written before this."
        )

    from interface.messages import send_manager_message

    send_manager_message(
        actor.contact_id,
        actor.contact_id,
        first_message,
        sender_label="your previous session",
    )
    return (
        f"New session started ({session_id}). Your first message has been "
        "queued and will open the new session."
    )


def cmd_do_nothing(args, actor) -> str:
    """Deliberately do nothing this turn, on the record.

    Running at least one command every turn is mandatory. This is how a Silicon
    says "I considered it and there is genuinely nothing to do" rather than
    simply going quiet, which is indistinguishable from failing.
    """
    reason = str(args.reason or "").strip()
    if not reason:
        raise _error(
            "Doing nothing needs a --reason. Say why there is nothing worth doing."
        )
    return ""


def cmd_restart_silicon(args, actor) -> str:
    reason = str(args.reason or "").strip()
    if not reason:
        raise _error(
            "Restarting needs a --reason. Say what core change needs it."
        )
    payload = {
        "reason": reason,
        "requested_by": actor.contact_id,
        "requested_at": time.time(),
    }
    try:
        with open(RESTART_REQUEST_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError as exc:
        raise _error(f"Could not request a restart: {exc}")
    try:
        from interface import notify_runtime_activity

        notify_runtime_activity()
    except Exception:
        pass
    return (
        "Restart requested. Silicon will restart shortly and tell you once it "
        "is back. Anything you have not written down will not survive it."
    )


def add_parser(subparsers, parser_cls):
    session = subparsers.add_parser(
        "start-new-session",
        help="start a fresh session, carrying one message across",
    )
    session.add_argument(
        "--first-message",
        dest="first_message",
        help="the message that opens the new session",
    )
    session.set_defaults(_handler=cmd_start_new_session)

    nothing = subparsers.add_parser(
        "do-nothing", help="deliberately do nothing this turn, with a reason"
    )
    nothing.add_argument("--reason", help="why there is nothing to do")
    nothing.set_defaults(_handler=cmd_do_nothing)

    restart = subparsers.add_parser(
        "restart-silicon", help="restart Silicon so a core change takes effect"
    )
    restart.add_argument("--reason", help="what change needs the restart")
    restart.set_defaults(_handler=cmd_restart_silicon)
