"""Talking: `send`, `see`, `bundle`, `transcribe`, `request-lords`.

`send` is how the Silicon talks, and it goes straight into the named contact's
Interface DM. There used to be a manager per contact, so reaching anyone else
meant handing the message to *their* manager and hoping it passed it on — four
sessions between "ask silicon B for X" and anyone starting on X. There is one
session now, so "I" is the Silicon and one hop is the whole journey.

Workers address the session as `manager`, and the session addresses a running
worker by its worker id.
"""
from __future__ import annotations

import os

from helpers.paths import STATE_DIR
from datetime import datetime, timezone

from iwantto import mailbox, message_log
from iwantto.routing import RoutingError, resolve_target

MANAGER_TARGET = "manager"


def _error(message):
    from iwantto.cli import CommandError

    return CommandError(message)


# --- helpers ---------------------------------------------------------------


def _compose_voice(text: str, direction: str, gender: str) -> str:
    """Fold direction and gender into the transcript the TTS model receives.

    Gemini TTS takes one prompt: a directorial context block followed by the
    transcript (see prompts/VOICE_DIRECTION.md). The Interface `tts` call
    accepts exactly that one string, so direction and gender are composed into
    it rather than passed as separate transport fields.
    """
    notes = []
    if gender:
        notes.append(f"Voice: {gender}.")
    if direction:
        notes.append(direction.strip())
    if not notes:
        return text
    return "# DIRECTION\n" + " ".join(notes) + "\n\n# TRANSCRIPT\n" + text


def _build_message(args) -> tuple[str, str]:
    """Return (interface_message, kind) from the --text/--file/--voice flags."""
    text = str(getattr(args, "text", "") or "")
    file_path = str(getattr(args, "file", "") or "")
    voice = str(getattr(args, "voice", "") or "")
    caption = str(getattr(args, "caption", "") or "")

    provided = [bool(text), bool(file_path), bool(voice)]
    if sum(provided) == 0:
        raise _error("Say what you want to send: --text, --file, or --voice.")
    if sum(provided) > 1:
        raise _error("Send one thing at a time: --text, --file, or --voice.")

    if text:
        return text, "text"
    if voice:
        composed = _compose_voice(
            voice,
            str(getattr(args, "voice_direction", "") or ""),
            str(getattr(args, "voice_gender", "") or ""),
        )
        return f"[voice={composed}]", "voice"

    path = os.path.abspath(os.path.expanduser(file_path))
    if not os.path.exists(path):
        raise _error(f"No file at {path}.")
    prefix = f"{caption}\n" if caption else ""
    return f"{prefix}[file={path}]", "file"


def _my_worker(actor, worker_id: str) -> dict:
    """The worker with this id, if it belongs to the caller's manager."""
    try:
        from worker.registry import _get_worker_record

        record = _get_worker_record(worker_id)
    except Exception:
        return {}
    if not isinstance(record, dict):
        return {}
    if str(record.get("carbon_id") or "") != actor.contact_id:
        return {}
    return record


# --- send ------------------------------------------------------------------


def _send_to_manager(actor, message: str) -> str:
    """A worker speaking to the manager that started it."""
    if not actor.is_worker:
        raise _error(
            "Only a worker sends to `manager`. You are the manager — name the "
            "carbon or silicon you want to reach."
        )
    sender = f"{actor.worker_type or 'worker'} worker `{actor.actor_id}`"
    mailbox.deliver("manager", actor.contact_id, sender, message)
    try:
        from interface.messages import send_manager_message

        send_manager_message(
            actor.actor_id,
            actor.contact_id,
            message,
            sender_label=sender,
        )
    except Exception as exc:
        return (
            f"Left the message for your manager, but could not wake it: {exc}. "
            "It will be read the next time your manager runs."
        )
    return f"Sent to your manager. They will see it as a message from {sender}."


def _send_to_worker(actor, worker_id: str, record: dict, message: str) -> str:
    """A manager answering a worker mid-task, without interrupting it."""
    sender = "the Silicon"
    mailbox.deliver("worker", worker_id, sender, message)
    state = "running" if record.get("state") == "active" else str(
        record.get("state") or "known"
    )
    return (
        f"Sent to worker `{worker_id}` ({state}). It will read this the next "
        "time it runs an iwantto command. It has not been stopped."
    )


def _send_direct(actor, target, message: str) -> str:
    """Straight into the target's Interface DM. The only path there is.

    A send never closes a work. Talking and finishing are separate acts now —
    `iwantto work --completed` settles the durable card, and it says so in the
    chat itself, so a message that happens to sound conclusive cannot quietly
    end something.
    """
    from interface import ensure_contact_for_target, reply_contact

    try:
        ensure_contact_for_target(target.kind, target.fixed_id)
    except Exception as exc:
        raise _error(f"Could not reach {target.label}: {exc}")

    status = reply_contact(message, target.fixed_id, work_continues=True)
    if str(status).startswith("Error"):
        raise _error(f"Could not send to {target.label}: {status}")
    return f"Sent to {target.label}. ({status})"


def cmd_send(args, actor) -> str:
    raw_target = str(args.target or "").strip()
    message, _kind = _build_message(args)

    if raw_target == MANAGER_TARGET:
        return _send_to_manager(actor, message)

    worker_record = _my_worker(actor, raw_target)
    if worker_record:
        return _send_to_worker(actor, raw_target, worker_record, message)

    kind_hint = "carbon" if args.carbon else "silicon" if args.silicon else ""
    try:
        target = resolve_target(raw_target, kind_hint=kind_hint)
    except RoutingError as exc:
        raise _error(str(exc))

    # A worker shares the session's contact id but not its voice: it reports to
    # the session and the session decides what anybody outside hears.
    if actor.is_worker:
        raise _error(
            f"A worker does not talk to {target.label} directly. Send it to "
            "`manager` and say what you need — the Silicon decides what goes "
            "out and who it goes to."
        )
    return _send_direct(actor, target, message)


# --- see -------------------------------------------------------------------


def _parse_dt(value: str, flag: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise _error(f"{flag} needs an ISO 8601 datetime.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _error(
            f"{flag} is not ISO 8601: {raw!r}. Try 2026-08-09T14:00:00Z."
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def cmd_see(args, actor) -> str:
    if args.id:
        contact_id, entry = message_log.find(args.id)
        if not entry:
            return f"No message with msgid {args.id} in my records."
        return message_log.format_entry(entry, contact_id=contact_id)

    if not args.target:
        raise _error(
            "Say whose messages: `iwantto see --last 10 <carbonid/siliconid>`, "
            "or look one up with `--id <msgid>`."
        )
    try:
        target = resolve_target(
            args.target,
            kind_hint="carbon" if args.carbon else "silicon" if args.silicon else "",
        )
    except RoutingError as exc:
        raise _error(str(exc))

    if args.unread:
        entries = message_log.unanswered(target.fixed_id)
        if not entries:
            return f"{target.label} has replied to everything I sent."
        body = message_log.format_history(entries, contact_id=target.fixed_id)
        return (
            f"{len(entries)} message(s) sent to {target.label} with no reply "
            f"since:\n{body}"
        )

    if args.dt_from or args.dt_to:
        entries = message_log.history(
            target.fixed_id,
            dt_from=_parse_dt(args.dt_from, "--dt-from") if args.dt_from else None,
            dt_to=_parse_dt(args.dt_to, "--dt-to") if args.dt_to else None,
        )
        return message_log.format_history(entries, contact_id=target.fixed_id)

    limit = int(args.last or 20)
    if limit <= 0:
        raise _error("--last needs a positive number.")
    entries = message_log.history(target.fixed_id, limit=limit)
    return message_log.format_history(entries, contact_id=target.fixed_id)


# --- bundle-unread ---------------------------------------------------------


def cmd_bundle(args, actor) -> str:
    """Retract a named range of messages and replace it with one summary.

    Take-back is the mechanism: each message this Silicon sent in the range is
    withdrawn, then the summary is sent in their place, so the contact opens the
    conversation to one readable message instead of eleven.

    The range is named rather than inferred, because the useful case is a carbon
    who *has* seen the pile and still not replied — "unanswered" would not have
    found it. Their own messages inside the range are left alone; withdrawing
    somebody else's words is not ours to do.
    """
    from interface import take_back_event

    try:
        target = resolve_target(
            args.target,
            kind_hint="carbon" if args.carbon else "silicon" if args.silicon else "",
        )
    except RoutingError as exc:
        raise _error(str(exc))

    message, _kind = _build_message(args)
    if not getattr(args, "from_id", None):
        raise _error(
            "Which messages? `iwantto bundle <target> --from <msgid> --to "
            "<msgid>`. Find the msgids with `iwantto see --last 20 <target>`."
        )
    entries, error = message_log.span(
        target.fixed_id, args.from_id, getattr(args, "to_id", "") or ""
    )
    if error:
        raise _error(error)

    mine = [entry for entry in entries if entry.get("direction") == message_log.OUT]
    if not mine:
        raise _error(
            f"Nothing of mine in that range — all {len(entries)} message(s) are "
            f"{target.label}'s. Bundling replaces what I said, not what they did."
        )

    withdrawn, failures = [], []
    for entry in mine:
        event_id = str(entry.get("event_id") or "")
        if not event_id:
            continue
        # Forced, because the pile worth bundling is the one they have already
        # read. An ordinary take-back refuses a seen event, which used to be the
        # honest answer and is now the whole case this command exists for.
        status = take_back_event(
            event_id, reason="bundled into a summary", force=True
        )
        if str(status).startswith("Error"):
            failures.append(f"{event_id}: {status}")
        else:
            withdrawn.append(event_id)

    sent = _send_direct(actor, target, message)

    lines = [
        f"Bundled {len(withdrawn)} of my {len(mine)} message(s) to "
        f"{target.label} into one.",
        sent,
    ]
    theirs = len(entries) - len(mine)
    if theirs:
        lines.append(f"Left {theirs} of their message(s) in place.")
    if failures:
        lines.append(
            f"{len(failures)} could not be taken back (too old, or Glass "
            "refused): " + "; ".join(failures[:5])
        )
    return "\n".join(lines)


# --- transcribe ------------------------------------------------------------


def cmd_transcribe(args, actor) -> str:
    from interface import InterfaceClient

    path = os.path.abspath(os.path.expanduser(str(args.path or "")))
    if not os.path.exists(path):
        raise _error(f"No file at {path}.")
    payload = InterfaceClient().stt(path)
    if isinstance(payload, dict):
        for key in ("text", "transcript", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(payload or "").strip() or "No transcript came back."


# --- request-lords ---------------------------------------------------------


def cmd_request_lords(args, actor) -> str:
    """Send a feature request to the lords at Team of Silicons."""
    from helpers.state import update_json

    title = str(args.title or "").strip()
    description = str(args.description or "").strip()
    if not title or not description:
        raise _error("A request needs both --title and --description.")

    body = {
        "title": title,
        "description": description,
        "requested_by_contact_id": actor.contact_id,
    }
    try:
        from interface.config import silicon_api_post

        silicon_api_post("/api/v1/silicons/me/feature-requests", body)
        return f"Sent to the lords: {title!r}."
    except Exception as exc:
        # Never lose a request because the endpoint was unreachable. Keep it
        # durably so it can be re-sent, and say plainly that it has not landed.
        path = os.path.join(
            os.fspath(STATE_DIR), "lord_requests.json"
        )

        def update(state):
            state.setdefault("requests", []).append(body)

        try:
            update_json(path, {"version": 1, "requests": []}, update)
        except Exception:
            raise _error(f"Could not send the request and could not save it: {exc}")
        return (
            f"Could not reach the lords ({exc}). Saved your request {title!r} "
            "locally so it is not lost — it has NOT been delivered yet."
        )


# --- parser wiring ---------------------------------------------------------


def _add_target_kind_flags(parser):
    parser.add_argument(
        "--carbon",
        action="store_true",
        help="the target is a carbon (only needed if the name is ambiguous)",
    )
    parser.add_argument(
        "--silicon",
        action="store_true",
        help="the target is a silicon (only needed if the name is ambiguous)",
    )


def _add_body_flags(parser):
    parser.add_argument("--text", help="message text (full markdown)")
    parser.add_argument("--file", help="path to a file to send")
    parser.add_argument("--caption", help="caption to send with --file")
    parser.add_argument("--voice", help="text to speak as a voice message")
    parser.add_argument(
        "--voice-direction",
        dest="voice_direction",
        help="how it should be performed (see prompts/VOICE_DIRECTION.md)",
    )
    parser.add_argument(
        "--voice-gender",
        dest="voice_gender",
        choices=["male", "female"],
        help="voice to speak with",
    )


def add_parser(subparsers, parser_cls):
    send = subparsers.add_parser(
        "send",
        help="message a carbon, a silicon, your manager, or one of your workers",
        description="Send a message straight to whoever you name. There are no "
        "hops: the id you type is the chat it lands in.",
    )
    send.add_argument(
        "target",
        help="carbon id, silicon id, `manager` (workers), or a worker id",
    )
    _add_body_flags(send)
    _add_target_kind_flags(send)
    send.set_defaults(_handler=cmd_send)

    see = subparsers.add_parser(
        "see",
        help="read message history, unread messages, or one message by id",
    )
    see.add_argument("target", nargs="?", help="carbon id or silicon id")
    see.add_argument("--last", type=int, help="show the last N messages")
    see.add_argument("--dt-from", dest="dt_from", help="ISO 8601 start")
    see.add_argument("--dt-to", dest="dt_to", help="ISO 8601 end")
    see.add_argument(
        "--unread",
        action="store_true",
        help="messages you sent that they have not replied to",
    )
    see.add_argument("--id", help="look up one message by msgid")
    _add_target_kind_flags(see)
    see.set_defaults(_handler=cmd_see)

    bundle = subparsers.add_parser(
        "bundle",
        help="take back a range of your messages and replace them with one summary",
        description="For when they have seen the pile and still not replied. "
        "Name the range with msgids from `iwantto see`.",
    )
    bundle.add_argument("target", help="carbon id or silicon id")
    bundle.add_argument(
        "--from",
        dest="from_id",
        metavar="MSGID",
        help="first message of the range",
    )
    bundle.add_argument(
        "--to",
        dest="to_id",
        metavar="MSGID",
        help="last message of the range (defaults to the most recent)",
    )
    _add_body_flags(bundle)
    _add_target_kind_flags(bundle)
    bundle.set_defaults(_handler=cmd_bundle)

    transcribe = subparsers.add_parser(
        "transcribe", help="transcribe an audio or video file"
    )
    transcribe.add_argument("path", help="path to the audio or video file")
    transcribe.set_defaults(_handler=cmd_transcribe)

    lords = subparsers.add_parser(
        "request-lords",
        help="ask the lords at Team of Silicons for a capability you are missing",
    )
    lords.add_argument("--title", required=True)
    lords.add_argument("--description", required=True)
    lords.set_defaults(_handler=cmd_request_lords)
