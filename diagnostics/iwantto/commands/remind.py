"""`iwantto remind` — the only way a Silicon acts without being spoken to.

Glass schedules on five-field cron expressions, so a one-off reminder (`--in
30m`, `--at <ISO>`) is stored as a cron that matches exactly one minute and is
deleted once it has fired. The reaper that deletes it runs on the event loop;
see :func:`reap_fired_reminders`.

Reminder ids are local and stable. Glass cron ids are not, because changing who
a reminder is for means deleting and recreating it — Glass's cron update cannot
change targets. Holding our own id means `--id` keeps working across that.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from diagnostics.iwantto.routing import RoutingError, resolve_target
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
    from diagnostics.iwantto.cli import CommandError

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


def _targets_for(names, kind_hint: str = "") -> list:
    targets = []
    for name in names:
        try:
            target = resolve_target(name, kind_hint=kind_hint)
        except RoutingError as exc:
            raise _error(str(exc))
        targets.append({"kind": target.kind, "id": target.fixed_id})
    return targets


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
            "`iwantto remind <someone> --list`."
        )
    return entry


def _describe(reminder_id: str, entry: dict) -> str:
    who = ", ".join(
        f"{target.get('kind')}:{target.get('id')}"
        for target in entry.get("targets") or []
    )
    when = (
        f"once at {entry.get('fire_at_iso')}"
        if entry.get("one_shot")
        else f"cron {entry.get('trigger')} ({entry.get('timezone')})"
    )
    task = " ".join(str(entry.get("task") or "").split())[:160]
    return f"{reminder_id} — {when} → {who}\n    {task}"


# --- create ----------------------------------------------------------------


def _create(args, actor) -> str:
    text = str(args.text or "").strip()
    if not text:
        raise _error("A reminder needs --text: what should you be reminded of?")

    # Work out *when* before *who*: the schedule is a local parse, so a
    # malformed --in is reported without a contact lookup first.
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

    kind_hint = "carbon" if args.carbon else "silicon" if args.silicon else ""
    targets = _targets_for([args.target], kind_hint)

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


def _recreate(reminder_id: str, entry: dict, targets: list) -> None:
    """Glass cannot change a cron's targets, so replace the cron in place."""
    _delete_glass_cron(str(entry.get("cron_id") or ""))
    cron_id = _create_glass_cron(
        str(entry.get("trigger") or ""),
        str(entry.get("task") or ""),
        targets,
        str(entry.get("timezone") or "UTC"),
    )
    updated = dict(entry)
    updated["cron_id"] = cron_id
    updated["targets"] = targets
    _store(reminder_id, updated)


def _include(args, actor) -> str:
    entry = _get(args.id)
    added = _targets_for([args.include])[0]
    targets = list(entry.get("targets") or [])
    if any(
        target.get("id") == added["id"] and target.get("kind") == added["kind"]
        for target in targets
    ):
        return f"{added['id']} is already on reminder {args.id}."
    targets.append(added)
    _recreate(args.id, entry, targets)
    return f"Added {added['kind']}:{added['id']} to reminder {args.id}."


def _exclude(args, actor) -> str:
    entry = _get(args.id)
    removed = _targets_for([args.exclude])[0]
    targets = [
        target
        for target in entry.get("targets") or []
        if not (
            target.get("id") == removed["id"]
            and target.get("kind") == removed["kind"]
        )
    ]
    if len(targets) == len(entry.get("targets") or []):
        return f"{removed['id']} was not on reminder {args.id}."
    if not targets:
        return _delete_reminder(args.id, entry) + " (its last target was removed)"
    _recreate(args.id, entry, targets)
    return f"Removed {removed['kind']}:{removed['id']} from reminder {args.id}."


def _delete_reminder(reminder_id: str, entry: dict) -> str:
    _delete_glass_cron(str(entry.get("cron_id") or ""))

    def update(state):
        state.setdefault("reminders", {}).pop(reminder_id, None)

    update_json(REMINDERS_FILE, _default(), update)
    return f"Deleted reminder {reminder_id}."


def _list(args, actor) -> str:
    kind_hint = "carbon" if args.carbon else "silicon" if args.silicon else ""
    try:
        target = resolve_target(args.target, kind_hint=kind_hint)
    except RoutingError as exc:
        raise _error(str(exc))
    matching = {
        reminder_id: entry
        for reminder_id, entry in _reminders().items()
        if any(
            item.get("id") == target.fixed_id
            for item in entry.get("targets") or []
        )
    }
    if not matching:
        return f"No reminders set for {target.label}."
    return "\n".join(
        _describe(reminder_id, entry)
        for reminder_id, entry in sorted(matching.items())
    )


def cmd_remind(args, actor) -> str:
    if args.id:
        entry = _get(args.id)
        if args.delete:
            return _delete_reminder(args.id, entry)
        if args.include:
            return _include(args, actor)
        if args.exclude:
            return _exclude(args, actor)
        return _describe(args.id, entry)

    if not args.target:
        raise _error(
            "Who is the reminder for? `iwantto remind <carbonid/siliconid> "
            '--in 2h --text "..."`, or act on one with `--id <reminder-id>`.'
        )
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
        "remind",
        help="set a one-off or recurring reminder — your only way to be proactive",
    )
    parser.add_argument("target", nargs="?", help="carbon id or silicon id")
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
    parser.add_argument("--list", action="store_true", help="list their reminders")
    parser.add_argument("--id", help="an existing reminder id")
    parser.add_argument("--include", help="add a carbon or silicon to it")
    parser.add_argument("--exclude", help="remove a carbon or silicon from it")
    parser.add_argument("--delete", action="store_true", help="delete it")
    parser.add_argument("--carbon", action="store_true")
    parser.add_argument("--silicon", action="store_true")
    parser.set_defaults(_handler=cmd_remind)
