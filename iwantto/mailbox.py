"""Mid-run mail between a manager and its workers.

A running worker is a subprocess in the middle of a task; there is no way to
push a line into it. But it comes back to `iwantto` constantly — to send a
message, log work, ask a question — and that is the opening. Mail addressed to
an actor is delivered as a trailer on the next command it runs, so a manager
can answer a worker's question without stopping the worker, and the worker
picks the answer up the next time it acts.

Mail is delivered once and then gone. It is a nudge inside a live run, not a
durable queue: anything that must survive the run belongs in the manager
message queue instead.
"""
from __future__ import annotations

import os
import time

from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.state import update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
MAILBOX_FILE = os.path.join(
    os.fspath(STATE_DIR), "iwantto_mailbox.json"
)

MAX_PER_BOX = 50
# Undelivered mail is only interesting while its run is plausibly alive.
TTL_SECONDS = 24 * 60 * 60


def _key(kind: str, actor_id: str) -> str:
    return f"{kind}:{actor_id}"


def _default() -> dict:
    return {"version": 1, "boxes": {}}


def _prune(boxes: dict, now: float) -> None:
    for key, items in list(boxes.items()):
        if not isinstance(items, list):
            boxes.pop(key, None)
            continue
        kept = [
            item
            for item in items
            if isinstance(item, dict)
            and now - float(item.get("at") or 0.0) < TTL_SECONDS
        ]
        del kept[:-MAX_PER_BOX]
        if kept:
            boxes[key] = kept
        else:
            boxes.pop(key, None)


def deliver(kind: str, actor_id: str, sender: str, message: str) -> None:
    """Leave mail for an actor to pick up on its next command."""
    kind = str(kind or "")
    actor_id = str(actor_id or "")
    if not kind or not actor_id:
        return
    now = time.time()
    entry = {"from": str(sender or ""), "message": str(message or ""), "at": now}

    def update(state):
        boxes = state.setdefault("boxes", {})
        _prune(boxes, now)
        boxes.setdefault(_key(kind, actor_id), []).append(entry)

    update_json(MAILBOX_FILE, _default(), update)


def drain(kind: str, actor_id: str) -> list:
    """Take everything waiting for this actor, leaving the box empty."""
    kind = str(kind or "")
    actor_id = str(actor_id or "")
    if not kind or not actor_id:
        return []
    now = time.time()

    def update(state):
        boxes = state.setdefault("boxes", {})
        _prune(boxes, now)
        return boxes.pop(_key(kind, actor_id), [])

    try:
        items = update_json(MAILBOX_FILE, _default(), update)
    except Exception:
        return []
    return [item for item in (items or []) if isinstance(item, dict)]


def format_mail(items: list) -> str:
    """Render pending mail as a trailer, or an empty string when there is none."""
    if not items:
        return ""
    lines = ["", "--- You have messages ---"]
    for item in items:
        sender = str(item.get("from") or "someone")
        lines.append(f"From {sender}: {item.get('message') or ''}")
    return "\n".join(lines)
