"""`iwantto remember` — the only way a Silicon acts without being spoken to.

A reminder is for the Silicon itself. It used to take a target, because there was
a manager per contact and one could poke another's; there is one session now, so
"who is this for" has exactly one answer and asking it was noise.

Glass schedules on five-field cron expressions, so a one-off reminder (`--in
30m`, `--at <ISO>`) is stored as a cron that matches exactly one minute and is
deleted once it has fired. The reaper that deletes it runs on the event loop;
see :func:`reap_fired_reminders`.

Reminder ids are local and stable, and stay that way: Glass cron ids are not
guaranteed across a recreate, so holding our own id means `--id` keeps working.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.state import read_json, update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
REMINDERS_FILE = os.path.join(
    os.fspath(STATE_DIR), "iwantto_reminders.json"
)

RELATIVE_RE = re.compile(r"^(\d+)([mhd])$")
UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
# A one-shot cron is only deleted once its minute is safely behind us, so a
# slow tick cannot delete it before Glass has fired it.
REAP_GRACE_SECONDS = 180


def _error(message):
    from iwantto.cli import CommandError

    return CommandError(message)


def _default() -> dict:
    return {"version": 1, "reminders": {}}


def _client():
    from interface import InterfaceClient

    return InterfaceClient()


def _parse_relative(value: str) -> datetime:
    match = RELATIVE_RE.match(str(value or "").strip().lower())
    if not match:
        raise _error(
            f"--in takes a number then m, h, or d — like 2m, 3h, 4d. Got {value!r}. "
            "Seconds, months and years are not supported."
        )
    amount, unit = int(match.group(1)), match.group(2)
    if amount <= 0:
        raise _error("--in needs a positive amount of time.")
    return datetime.now(timezone.utc) + timedelta(
        seconds=amount * UNIT_SECONDS[unit]
    )


def _parse_at(value: str) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _error(
            f"--at needs an ISO 8601 datetime, like 2026-08-09T15:30:00Z. Got {raw!r}."
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _one_shot_trigger(when: datetime) -> str:
    """A cron expression that matches exactly one minute of one day."""
    moment = when.astimezone(timezone.utc)
    return f"{moment.minute} {moment.hour} {moment.day} {moment.month} *"


def _own_target() -> list:
    """The Silicon itself, as a Glass cron target.

    A reminder is stored in Glass so it survives a reinstall, and a Glass cron
    record needs somebody to be for. That somebody is us. :mod:`interface.cron`
    recognises our own id and fires the reminder into the session rather than
    trying to open a DM with ourselves.
    """
    from interface import get_own_profile

    silicon_id = str((get_own_profile() or {}).get("silicon_id") or "").strip()
    if not silicon_id:
        raise _error(
            "I do not know my own Glass identity yet, so I cannot store a "
            "reminder that would survive a restart. Try again once Glass has "
            "confirmed who I am."
        )
    return [{"kind": "silicon", "id": silicon_id}]


def _create_glass_cron(trigger: str, task: str, targets: list, timezone_name: str) -> str:
    from interface.cron import invalidate_cron_cache

    payload = _client().cron_create(trigger, task, targets)
    invalidate_cron_cache()
    if isinstance(payload, dict):
        for key in ("cron_id", "id"):
            value = payload.get(key)
            if value:
                return str(value)
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("cron_id", "id"):
                if data.get(key):
                    return str(data[key])
    return ""


def _delete_glass_cron(cron_id: str) -> None:
    if not cron_id:
        return
    try:
        from interface.cron import invalidate_cron_cache

        _client().cron_delete(cron_id)
        invalidate_cron_cache()
    except Exception:
        pass


def _store(reminder_id: str, entry: dict) -> None:
    def update(state):
        state.setdefault("reminders", {})[reminder_id] = entry

    update_json(REMINDERS_FILE, _default(), update)


def _reminders() -> dict:
    state = read_json(REMINDERS_FILE, _default())
    reminders = state.get("reminders")
    return reminders if isinstance(reminders, dict) else {}


def _get(reminder_id: str) -> dict:
    entry = _reminders().get(reminder_id)
    if not isinstance(entry, dict):
        raise _error(
            f"No reminder with id {reminder_id!r}. List them with "
            "`iwantto remember --list`."
        )
    return entry


def _describe(reminder_id: str, entry: dict) -> str:
    when = (
        f"once at {entry.get('fire_at_iso')}"
        if entry.get("one_shot")
        else f"cron {entry.get('trigger')} ({entry.get('timezone')})"
    )
    task = " ".join(str(entry.get("task") or "").split())[:160]
    return f"{reminder_id} — {when}\n    {task}"


# --- create ----------------------------------------------------------------


def _create(args, actor) -> str:
    text = str(args.text or "").strip()
    if not text:
        raise _error("A reminder needs --text: what should you be reminded of?")

    one_shot = False
    fire_at = None
    timezone_name = str(args.tz or "UTC")
    if args.cron:
        trigger = str(args.cron)
    elif args.at:
        fire_at = _parse_at(args.at)
        trigger = _one_shot_trigger(fire_at)
        one_shot = True
        timezone_name = "UTC"
    elif getattr(args, "in_", None):
        fire_at = _parse_relative(args.in_)
        trigger = _one_shot_trigger(fire_at)
        one_shot = True
        timezone_name = "UTC"
    else:
        raise _error(
            "Say when: --in 2m/3h/4d, --at <ISO 8601>, or --cron \"0 9 * * 1-5\"."
        )

    targets = _own_target()
    cron_id = _create_glass_cron(trigger, text, targets, timezone_name)
    reminder_id = f"r-{uuid.uuid4().hex[:10]}"
    _store(
        reminder_id,
        {
            "cron_id": cron_id,
            "trigger": trigger,
            "timezone": timezone_name,
            "task": text,
            "targets": targets,
            "one_shot": one_shot,
            "fire_at": fire_at.timestamp() if fire_at else None,
            "fire_at_iso": (
                fire_at.strftime("%Y-%m-%dT%H:%M:%SZ") if fire_at else ""
            ),
            "created_at": time.time(),
            "created_by": actor.contact_id,
        },
    )
    when = (
        f"once at {fire_at.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        if one_shot
        else f"on cron {trigger} ({timezone_name})"
    )
    return f"Reminder {reminder_id} set — {when}."


# --- manage ----------------------------------------------------------------


def _delete_reminder(reminder_id: str, entry: dict) -> str:
    _delete_glass_cron(str(entry.get("cron_id") or ""))

    def update(state):
        state.setdefault("reminders", {}).pop(reminder_id, None)

    update_json(REMINDERS_FILE, _default(), update)
    return f"Deleted reminder {reminder_id}."


def _list(args, actor) -> str:
    reminders = _reminders()
    if not reminders:
        return "You have no reminders set."
    return "\n".join(
        _describe(reminder_id, entry)
        for reminder_id, entry in sorted(reminders.items())
    )


def cmd_remember(args, actor) -> str:
    if args.id:
        entry = _get(args.id)
        if args.delete:
            return _delete_reminder(args.id, entry)
        return _describe(args.id, entry)

    if args.list:
        return _list(args, actor)
    return _create(args, actor)


# --- reaper (event loop) ---------------------------------------------------


def reap_fired_reminders() -> int:
    """Delete one-shot reminders whose moment has passed. Returns how many."""
    now = time.time()
    expired = [
        (reminder_id, entry)
        for reminder_id, entry in _reminders().items()
        if entry.get("one_shot")
        and isinstance(entry.get("fire_at"), (int, float))
        and now - float(entry["fire_at"]) > REAP_GRACE_SECONDS
    ]
    for reminder_id, entry in expired:
        try:
            _delete_reminder(reminder_id, entry)
        except Exception:
            continue
    return len(expired)


def add_parser(subparsers, parser_cls):
    parser = subparsers.add_parser(
        "remember",
        help="remind yourself later — your only way to be proactive",
        description="Reminders are for you. When one fires you are woken with "
        "its text and decide what to do about it.",
    )
    parser.add_argument(
        "--in",
        dest="in_",
        metavar="2m|3h|4d",
        help="remind this long from now",
    )
    parser.add_argument("--at", help="remind at an exact ISO 8601 datetime")
    parser.add_argument("--cron", help='recurring, e.g. "0 9 * * 1-5"')
    parser.add_argument("--tz", help="IANA zone for --cron, e.g. Asia/Dubai")
    parser.add_argument("--text", help="what to be reminded of")
    parser.add_argument("--list", action="store_true", help="list your reminders")
    parser.add_argument("--id", help="an existing reminder id")
    parser.add_argument("--delete", action="store_true", help="delete it")
    parser.set_defaults(_handler=cmd_remember)
