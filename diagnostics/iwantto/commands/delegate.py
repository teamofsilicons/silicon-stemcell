"""`iwantto delegate` — the manager's hands.

Workers are browser, terminal, and writer processes that run a task and report
back. Checking back is mandatory when starting one: a worker that nobody
returns to is a task that quietly stops existing, so `--checkback-in` is
required rather than optional.
"""
from __future__ import annotations

import os
import re

MINUTES_RE = re.compile(r"^(\d+)m?$")
WORKER_TYPES = ("browser", "terminal", "writer")


def _error(message):
    from diagnostics.iwantto.cli import CommandError

    return CommandError(message)


def _minutes(value: str, flag: str) -> int:
    match = MINUTES_RE.match(str(value or "").strip().lower())
    if not match:
        raise _error(
            f"{flag} takes whole minutes, like 15m. Got {value!r}. "
            "Only minutes are supported."
        )
    minutes = int(match.group(1))
    if minutes <= 0:
        raise _error(f"{flag} needs a positive number of minutes.")
    return minutes


def _set_checkback(worker_id: str, actor, minutes: int) -> str:
    from interface.cron.checkback import add_checkback

    add_checkback(worker_id, actor.contact_id, minutes)
    return f"Checkback set for {minutes} minute(s) from now."


def _new(args, actor) -> str:
    from worker.handler import start_worker

    worker_type = str(args.worker or "").lower()
    if worker_type not in WORKER_TYPES:
        raise _error(
            f"--worker must be one of {'/'.join(WORKER_TYPES)}. Got {args.worker!r}."
        )
    worker_id = str(args.id or "").strip()
    if not worker_id:
        raise _error("Give the worker an --id so you can check back on it.")
    task = str(args.task or "").strip()
    if not task:
        raise _error("A worker needs a --task.")
    if not args.checkback_in:
        raise _error(
            "--checkback-in is required. A worker nobody returns to is a task "
            'that quietly disappears. Try --checkback-in 15m.'
        )
    minutes = _minutes(args.checkback_in, "--checkback-in")
    if args.incognito and worker_type != "browser":
        raise _error("--incognito only applies to a browser worker.")

    status = start_worker(
        worker_id,
        task,
        worker_type,
        actor.contact_id,
        incognito=bool(args.incognito),
    )
    if str(status).startswith("Error"):
        raise _error(str(status))
    checkback = _set_checkback(worker_id, actor, minutes)
    return f"Started {worker_type} worker `{worker_id}`. {status} {checkback}"


def _restart(args, actor) -> str:
    from worker.handler import message_worker

    task = str(args.task or "").strip()
    if not task:
        raise _error("Restarting a worker needs a new --task.")
    status = message_worker(args.id, task, actor.contact_id)
    if str(status).startswith("Error"):
        raise _error(str(status))
    extra = ""
    if args.checkback_in:
        extra = " " + _set_checkback(
            args.id, actor, _minutes(args.checkback_in, "--checkback-in")
        )
    return f"Restarted worker `{args.id}`. {status}{extra}"


def _search_archives(actor, term: str) -> str:
    """Rank finished worker runs by how often the term appears in them."""
    from worker.handler import OUTPUTS_DIR, _load_archive_meta

    needle = str(term or "").strip().lower()
    if not needle:
        raise _error("--search needs something to search for.")
    scored = []
    for archive_id, info in (_load_archive_meta() or {}).items():
        if not isinstance(info, dict) or info.get("carbon_id") != actor.contact_id:
            continue
        path = os.path.join(OUTPUTS_DIR, f"{archive_id}.txt")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                body = handle.read()
        except OSError:
            continue
        score = body.lower().count(needle)
        if not score:
            continue
        excerpt = ""
        position = body.lower().find(needle)
        if position >= 0:
            start = max(0, position - 80)
            excerpt = " ".join(body[start : position + 160].split())
        scored.append((score, archive_id, info, excerpt))
    if not scored:
        return f"No finished worker runs mention {term!r}."
    scored.sort(reverse=True)
    lines = [f"Worker runs mentioning {term!r}, most relevant first:"]
    for score, archive_id, info, excerpt in scored[:20]:
        lines.append(
            f"  {archive_id} ({info.get('worker_type', '?')}, {score} hit(s))"
            f"\n      …{excerpt}…"
        )
    return "\n".join(lines)


def cmd_delegate(args, actor) -> str:
    from worker.handler import (
        get_worker_status,
        list_active,
        list_archive,
        read_archive,
        stop_worker,
    )

    if args.archive:
        if args.search:
            return _search_archives(actor, args.search)
        listing = list_archive(actor.contact_id)
        if args.last:
            lines = str(listing).splitlines()
            listing = "\n".join(lines[: int(args.last) + 1])
        return str(listing)

    if args.list:
        return str(list_active(actor.contact_id))

    if args.worker:
        return _new(args, actor)

    if not args.id:
        raise _error(
            "Which worker? Start one with `--worker <type> --id <id> --task "
            '"..." --checkback-in 15m`, or list them with `--list`.'
        )

    if args.progress:
        return str(get_worker_status(args.id, actor.contact_id))
    if args.stop:
        return str(stop_worker(args.id, actor.contact_id))
    if args.restart:
        return _restart(args, actor)
    if args.read:
        return str(read_archive(args.id, actor.contact_id))
    if args.checkback_in:
        return _set_checkback(
            args.id, actor, _minutes(args.checkback_in, "--checkback-in")
        )

    raise _error(
        f"Nothing to do with worker `{args.id}`. Try --progress, --stop, "
        "--restart --task \"...\", or --checkback-in 15m."
    )


def add_parser(subparsers, parser_cls):
    parser = subparsers.add_parser(
        "delegate", help="start and manage your workers"
    )
    parser.add_argument(
        "--worker", choices=list(WORKER_TYPES), help="start a worker of this type"
    )
    parser.add_argument("--id", help="worker id")
    parser.add_argument("--task", help="what the worker should do")
    parser.add_argument(
        "--checkback-in",
        dest="checkback_in",
        metavar="Nm",
        help="remind yourself to check on it in N minutes (required for a new worker)",
    )
    parser.add_argument(
        "--incognito",
        action="store_true",
        help="browser worker only: run outside the default profile",
    )
    parser.add_argument("--list", action="store_true", help="list active workers")
    parser.add_argument("--progress", action="store_true", help="how is it going")
    parser.add_argument("--stop", action="store_true", help="stop it mid-task")
    parser.add_argument("--restart", action="store_true", help="restart with a new task")
    parser.add_argument("--archive", action="store_true", help="finished workers")
    parser.add_argument("--read", action="store_true", help="read one archived run")
    parser.add_argument("--last", type=int, help="with --archive: the last N")
    parser.add_argument("--search", help="with --archive: rank runs by a term")
    parser.set_defaults(_handler=cmd_delegate)
