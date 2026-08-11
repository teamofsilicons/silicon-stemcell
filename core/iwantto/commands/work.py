"""`iwantto work` — big work, visible to the carbon while it happens.

The command exposes three levels: a **work**, its **tasks**, and their
**subtasks**. Glass stores two — a task with a flat list of todos — so the
nesting is held here, in a local structure index, and flattened on the way out.
The index owns ids and parentage; Glass owns what the carbon sees.

Subtask ids are written ``<task>.<n>`` (``2.1``) so ``--subtask 2.1`` names one
unambiguously without also having to say which task it belongs to.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from helpers.paths import DATA_ROOT
from helpers.state import read_json, update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
WORK_FILE = os.path.join(
    PROJECT_ROOT, "core", "interface_state", "iwantto_work.json"
)

NOT_STARTED = "yet_to_start"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"


def _error(message):
    from core.iwantto.cli import CommandError

    return CommandError(message)


def _default() -> dict:
    return {"version": 1, "works": {}}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _works() -> dict:
    state = read_json(WORK_FILE, _default())
    works = state.get("works")
    return works if isinstance(works, dict) else {}


def _get_work(work_id: str) -> dict:
    work = _works().get(work_id)
    if not isinstance(work, dict):
        raise _error(
            f"No work with id {work_id!r}. Start it with "
            f'`iwantto work --new --id "{work_id}" --name "..."`, or run '
            "`iwantto work --active` to see what is open."
        )
    return work


def _glass(actor, action: str, task_id: str, data: dict, **spec) -> str:
    """Push one change to Glass, or raise.

    A work only exists because the carbon can watch it. If the push fails, the
    carbon sees nothing, so reporting success would tell the manager it had
    communicated when it had not. Callers that have already written local state
    roll it back before this propagates.
    """
    from core.work_updates import execute_work_update

    payload = {"action": action, "task_id": task_id, "data": data}
    payload.update(spec)
    result = execute_work_update(payload, actor.contact_id)
    if str(result).startswith("Error"):
        raise _error(
            f"Your carbon was not updated — {action} failed: {result}. "
            "Nothing was changed."
        )
    return result


# --- work lifecycle --------------------------------------------------------


def _create_work(args, actor) -> str:
    work_id = args.id
    name = str(args.name or "").strip()
    if not name:
        raise _error("A new work needs a --name.")
    if work_id in _works():
        raise _error(
            f"Work {work_id!r} already exists. Ids must be unique among your "
            "current work. Pick another, or drop --new to change this one."
        )

    # Push before recording: a work the carbon cannot see is not a work.
    glass_result = _glass(
        actor, "task/create", work_id, {"task_id": work_id, "title": name}
    )

    def update(state):
        state.setdefault("works", {})[work_id] = {
            "name": name,
            "owner_contact_id": actor.contact_id,
            "created_at": time.time(),
            "created_at_iso": _now_iso(),
            "state": IN_PROGRESS,
            "completed_at_iso": "",
            "next_task_number": 1,
            "tasks": {},
        }

    update_json(WORK_FILE, _default(), update)
    return f"Started work {work_id!r}: {name}. It is running. ({glass_result})"


def _rename_work(args, actor) -> str:
    work = _get_work(args.id)
    name = str(args.name or "").strip()
    old = work.get("name") or ""
    glass_result = _glass(actor, "task/update", args.id, {"title": name})

    def update(state):
        state["works"][args.id]["name"] = name

    update_json(WORK_FILE, _default(), update)
    return f"Renamed {args.id!r} from {old!r} to {name!r}. ({glass_result})"


# --- tasks and subtasks ----------------------------------------------------


def _add_task(args, actor) -> str:
    _get_work(args.id)
    title = str(args.title or "").strip()
    description = str(args.description or "").strip()
    if not title or not description:
        raise _error("Every task needs a --title and a --description.")

    holder = {}

    def update(state):
        work = state["works"][args.id]
        number = str(work.get("next_task_number") or 1)
        work["next_task_number"] = int(number) + 1
        work.setdefault("tasks", {})[number] = {
            "title": title,
            "description": description,
            "state": NOT_STARTED,
            "todo_id": f"{args.id}-t{number}",
            "start_note": "",
            "end_note": "",
            "next_subtask_number": 1,
            "subtasks": {},
        }
        holder["number"] = number
        holder["todo_id"] = work["tasks"][number]["todo_id"]

    # The id has to be allocated before the push, so a failed push is rolled
    # back rather than leaving a task the carbon will never see.
    update_json(WORK_FILE, _default(), update)
    try:
        glass_result = _glass(
            actor,
            "todo/add",
            args.id,
            {
                "todo_id": holder["todo_id"],
                "title": title,
                "description": description,
                "state": NOT_STARTED,
            },
        )
    except Exception:
        _rollback_task(args.id, holder["number"])
        raise
    return (
        f"Added task {holder['number']} to {args.id!r}: {title}. "
        f"Start it with `--task {holder['number']} --start \"note\"`. "
        f"({glass_result})"
    )


def _rollback_task(work_id: str, number: str) -> None:
    def update(state):
        work = state.get("works", {}).get(work_id)
        if isinstance(work, dict):
            work.get("tasks", {}).pop(number, None)
            work["next_task_number"] = int(number)

    try:
        update_json(WORK_FILE, _default(), update)
    except Exception:
        pass


def _rollback_subtask(work_id: str, task_number: str, subtask_id: str) -> None:
    def update(state):
        task = state.get("works", {}).get(work_id, {}).get("tasks", {}).get(
            task_number
        )
        if isinstance(task, dict):
            task.get("subtasks", {}).pop(subtask_id, None)
            task["next_subtask_number"] = int(subtask_id.rsplit(".", 1)[-1])

    try:
        update_json(WORK_FILE, _default(), update)
    except Exception:
        pass


def _add_subtask(args, actor) -> str:
    work = _get_work(args.id)
    task_number = str(args.task or "")
    task = (work.get("tasks") or {}).get(task_number)
    if not isinstance(task, dict):
        raise _error(f"Work {args.id!r} has no task {task_number}.")
    title = str(args.title or "").strip()
    description = str(args.description or "").strip()
    if not title or not description:
        raise _error("Every subtask needs a --title and a --description.")

    holder = {}

    def update(state):
        entry = state["works"][args.id]["tasks"][task_number]
        number = str(entry.get("next_subtask_number") or 1)
        entry["next_subtask_number"] = int(number) + 1
        subtask_id = f"{task_number}.{number}"
        entry.setdefault("subtasks", {})[subtask_id] = {
            "title": title,
            "description": description,
            "state": NOT_STARTED,
            "todo_id": f"{args.id}-t{task_number}-s{number}",
            "start_note": "",
            "end_note": "",
        }
        holder["subtask_id"] = subtask_id
        holder["todo_id"] = entry["subtasks"][subtask_id]["todo_id"]

    update_json(WORK_FILE, _default(), update)
    try:
        glass_result = _glass(
            actor,
            "todo/add",
            args.id,
            {
                "todo_id": holder["todo_id"],
                # Glass keeps a flat todo list, so the parent is carried in the
                # title to keep the carbon's view readable.
                "title": f"{task.get('title')} › {title}",
                "description": description,
                "state": NOT_STARTED,
            },
        )
    except Exception:
        _rollback_subtask(args.id, task_number, holder["subtask_id"])
        raise
    return (
        f"Added subtask {holder['subtask_id']} under task {task_number}: "
        f"{title}. ({glass_result})"
    )


def _find_entry(work: dict, *, task: str = "", subtask: str = "") -> tuple[dict, str]:
    """Locate a task or subtask node and the label to report it by."""
    tasks = work.get("tasks") or {}
    if subtask:
        parent = subtask.split(".", 1)[0]
        entry = ((tasks.get(parent) or {}).get("subtasks") or {}).get(subtask)
        if not isinstance(entry, dict):
            raise _error(f"No subtask {subtask!r} in this work.")
        return entry, f"subtask {subtask}"
    entry = tasks.get(task)
    if not isinstance(entry, dict):
        raise _error(f"No task {task!r} in this work.")
    return entry, f"task {task}"


def _transition(args, actor, *, starting: bool) -> str:
    work = _get_work(args.id)
    task_number = str(args.task or "")
    subtask_id = str(args.subtask or "")
    note = str((args.start if starting else args.end) or "")
    entry, label = _find_entry(work, task=task_number, subtask=subtask_id)
    new_state = IN_PROGRESS if starting else COMPLETED
    note_field = "start_note" if starting else "end_note"
    previous = (entry.get("state"), entry.get(note_field))

    holder = {}

    def apply(state_value, note_value):
        def update(state):
            work_state = state["works"][args.id]
            if subtask_id:
                parent = subtask_id.split(".", 1)[0]
                node = work_state["tasks"][parent]["subtasks"][subtask_id]
            else:
                node = work_state["tasks"][task_number]
            node["state"] = state_value
            node[note_field] = note_value
            holder["todo_id"] = node.get("todo_id")

        update_json(WORK_FILE, _default(), update)

    apply(new_state, note)
    payload = {"state": new_state}
    if note:
        payload["note"] = note
    try:
        glass_result = _glass(
            actor,
            "todo/update",
            args.id,
            payload,
            todo_id=holder["todo_id"],
        )
    except Exception:
        apply(*previous)
        raise
    verb = "started" if starting else "finished"
    return f"{label.capitalize()} {verb}. ({glass_result})"


def _list_subtasks(args, actor) -> str:
    work = _get_work(args.id)
    task_number = str(args.task or "")
    task = (work.get("tasks") or {}).get(task_number)
    if not isinstance(task, dict):
        raise _error(f"Work {args.id!r} has no task {task_number}.")
    subtasks = task.get("subtasks") or {}
    if not subtasks:
        return f"Task {task_number} has no subtasks yet."
    lines = [f"Subtasks of task {task_number} ({task.get('title')}):"]
    for subtask_id, node in sorted(subtasks.items()):
        lines.append(
            f"  [{node.get('state')}] {subtask_id} — {node.get('title')}\n"
            f"      {node.get('description')}"
        )
    return "\n".join(lines)


def _expand(args, actor) -> str:
    work = _get_work(args.id)
    lines = [
        f"{args.id} — {work.get('name')}  [{work.get('state')}]",
        f"  started {work.get('created_at_iso')}",
    ]
    tasks = work.get("tasks") or {}
    if not tasks:
        lines.append("  (no tasks yet)")
    for number, task in sorted(tasks.items(), key=lambda item: int(item[0])):
        lines.append(f"  [{task.get('state')}] task {number} — {task.get('title')}")
        lines.append(f"      {task.get('description')}")
        if task.get("start_note"):
            lines.append(f"      start: {task['start_note']}")
        if task.get("end_note"):
            lines.append(f"      end: {task['end_note']}")
        for subtask_id, node in sorted((task.get("subtasks") or {}).items()):
            lines.append(
                f"      [{node.get('state')}] subtask {subtask_id} — {node.get('title')}"
            )
            lines.append(f"          {node.get('description')}")
            if node.get("start_note"):
                lines.append(f"          start: {node['start_note']}")
            if node.get("end_note"):
                lines.append(f"          end: {node['end_note']}")
    return "\n".join(lines)


# --- carbon-visible events -------------------------------------------------


def _dispatch_update(args, actor) -> str:
    _get_work(args.id)
    title = str(args.title or "").strip()
    description = str(args.description or "").strip()
    if not title or not description:
        raise _error("An update needs a --title and a --description.")
    glass_result = _glass(
        actor,
        "milestone",
        args.id,
        {"title": title, "body": description},
    )
    return f"Update sent to your carbon: {title!r}. ({glass_result})"


def _blocker(args, actor) -> str:
    _get_work(args.id)
    title = str(args.title or "").strip()
    description = str(args.description or "").strip()
    if not title or not description:
        raise _error("A blocker needs a --title and a --description.")
    glass_result = _glass(
        actor,
        "blocker/create",
        args.id,
        {"title": title, "body": description},
    )
    return (
        f"Blocker raised with your carbon: {title!r}. They have been notified. "
        f"({glass_result})"
    )


def _complete(args, actor) -> str:
    _get_work(args.id)
    title = str(args.title or "").strip()
    description = str(args.description or "").strip()
    if not title:
        raise _error("Completing a work needs a --title.")
    glass_result = _glass(
        actor,
        "task/complete",
        args.id,
        {"title": title, "body": description},
    )

    def update(state):
        work = state["works"][args.id]
        work["state"] = COMPLETED
        work["completed_at_iso"] = _now_iso()
        work["completed_at"] = time.time()

    update_json(WORK_FILE, _default(), update)
    return f"Work {args.id!r} completed: {title}. ({glass_result})"


# --- listing ---------------------------------------------------------------


def _summary_line(work_id: str, work: dict) -> str:
    tasks = work.get("tasks") or {}
    done = sum(1 for task in tasks.values() if task.get("state") == COMPLETED)
    owner = work.get("owner_contact_id") or "?"
    when = work.get("completed_at_iso") or work.get("created_at_iso") or ""
    return (
        f"[{work.get('state')}] {work_id} — {work.get('name')} "
        f"({done}/{len(tasks)} tasks) by manager of {owner} {when}"
    )


def active_works(contact_id: str = "") -> list:
    """Open works, optionally only those owned by one contact's manager."""
    return [
        (work_id, work)
        for work_id, work in sorted(_works().items())
        if isinstance(work, dict)
        and work.get("state") != COMPLETED
        and (not contact_id or work.get("owner_contact_id") == contact_id)
    ]


def _list_active(args, actor) -> str:
    by = str(args.by or "")
    if by:
        from core.iwantto.routing import RoutingError, resolve_target

        try:
            by = resolve_target(by).fixed_id
        except RoutingError as exc:
            raise _error(str(exc))
    entries = active_works(by)
    if not entries:
        return "No active work."
    return "\n".join(_summary_line(work_id, work) for work_id, work in entries)


def _list_last(args, actor) -> str:
    limit = int(args.last or 10)
    by = str(args.by or "")
    if by:
        from core.iwantto.routing import RoutingError, resolve_target

        try:
            by = resolve_target(by).fixed_id
        except RoutingError as exc:
            raise _error(str(exc))
    finished = [
        (work_id, work)
        for work_id, work in _works().items()
        if isinstance(work, dict)
        and work.get("state") == COMPLETED
        and (not by or work.get("owner_contact_id") == by)
    ]
    finished.sort(key=lambda item: float(item[1].get("completed_at") or 0.0))
    finished = finished[-limit:] if limit > 0 else finished
    if not finished:
        return "No completed work yet."
    return "\n".join(_summary_line(work_id, work) for work_id, work in finished)


# --- dispatch --------------------------------------------------------------


def cmd_work(args, actor) -> str:
    if args.active:
        return _list_active(args, actor)
    if args.last is not None:
        return _list_last(args, actor)

    if not args.id:
        raise _error(
            "Which work? Pass --id, or list them with `--active` / `--last N`."
        )
    if args.new:
        return _create_work(args, actor)

    if args.add_task:
        return _add_task(args, actor)
    if args.add_subtask:
        return _add_subtask(args, actor)
    if args.list_subtask:
        return _list_subtasks(args, actor)
    if args.expand:
        return _expand(args, actor)
    if args.dispatch_update:
        return _dispatch_update(args, actor)
    if args.blocker:
        return _blocker(args, actor)
    if args.completed:
        return _complete(args, actor)
    if args.start is not None:
        return _transition(args, actor, starting=True)
    if args.end is not None:
        return _transition(args, actor, starting=False)
    if args.name:
        return _rename_work(args, actor)

    raise _error(
        "Nothing to do for that work. Try --expand to see it, --add-task to "
        "extend it, or --dispatch-update to tell your carbon how it is going."
    )


def add_parser(subparsers, parser_cls):
    parser = subparsers.add_parser(
        "work",
        help="track big, long-running work your carbon can watch",
        description="A work holds tasks; tasks hold subtasks. Nothing is ever "
        "deleted — end it with a note saying why it was left undone.",
    )
    parser.add_argument("--id", help="the work id (unique among your open work)")
    parser.add_argument("--new", action="store_true", help="start a new work")
    parser.add_argument("--name", help="the work's name")
    parser.add_argument("--expand", action="store_true", help="show the whole tree")

    parser.add_argument("--add-task", dest="add_task", action="store_true")
    parser.add_argument("--add-subtask", dest="add_subtask", action="store_true")
    parser.add_argument(
        "--list-subtask", dest="list_subtask", action="store_true"
    )
    parser.add_argument("--task", help="task id within the work")
    parser.add_argument("--subtask", help="subtask id, written <task>.<n>")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument(
        "--start", nargs="?", const="", help="mark started, with a note"
    )
    parser.add_argument(
        "--end", nargs="?", const="", help="mark finished, with a note"
    )

    parser.add_argument(
        "--dispatch-update",
        dest="dispatch_update",
        action="store_true",
        help="tell your carbon how it is going (pings them)",
    )
    parser.add_argument(
        "--blocker",
        action="store_true",
        help="you need a decision from your carbon (pings them)",
    )
    parser.add_argument(
        "--completed", action="store_true", help="mark the whole work done"
    )

    parser.add_argument("--active", action="store_true", help="list open work")
    parser.add_argument("--last", type=int, help="list the last N completed works")
    parser.add_argument("--by", help="filter by a carbon's or silicon's manager")
    parser.set_defaults(_handler=cmd_work)
