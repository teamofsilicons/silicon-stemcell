"""What was said, to whom, and when.

Glass delivers events but does not let this Silicon ask "has she read my last
three messages yet?" — read receipts flow outward, not back. So the local
record is what `iwantto see` reads from: every message this Silicon sends and
every message it receives, keyed by the Interface event id, which is also the
``msgid`` a Silicon quotes.

"Unread" here means what the CLI reference means by it: messages I sent that
they have not answered. As the reference puts it — if someone hasn't replied,
chances are they haven't read them.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.state import read_json, update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
MESSAGES_DIR = os.path.join(
    os.fspath(STATE_DIR), "messages"
)

OUT = "out"
IN = "in"

# Enough history for a manager to reconstruct a conversation, bounded so a busy
# contact cannot grow the file without limit.
MAX_PER_CONTACT = 2000
MAX_BODY_CHARS = 8000


def _safe_contact(contact_id: str) -> str:
    """A contact id is a Glass identity, but it still names a file here."""
    raw = str(contact_id or "").strip()
    return "".join(
        char if (char.isalnum() or char in "._-") else "_" for char in raw
    )[:120]


def _path(contact_id: str) -> str:
    return os.path.join(MESSAGES_DIR, f"{_safe_contact(contact_id)}.json")


def _default() -> dict:
    return {"version": 1, "messages": []}


def _clip(body: str) -> str:
    text = str(body or "")
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS] + f"…(+{len(text) - MAX_BODY_CHARS} chars)"
    return text


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(
    contact_id: str,
    direction: str,
    *,
    event_id: str = "",
    body: str = "",
    kind: str = "text",
    sender: str = "",
) -> None:
    """Append one message. Best-effort: logging must never break delivery."""
    contact_id = str(contact_id or "")
    if not contact_id:
        return
    now = time.time()
    entry = {
        "event_id": str(event_id or ""),
        "direction": OUT if direction == OUT else IN,
        "kind": str(kind or "text"),
        "body": _clip(body),
        "at": now,
        "at_iso": _iso(now),
        "sender": str(sender or ""),
    }

    def update(state):
        messages = state.setdefault("messages", [])
        if entry["event_id"] and any(
            item.get("event_id") == entry["event_id"]
            for item in messages
            if isinstance(item, dict)
        ):
            return
        messages.append(entry)
        del messages[:-MAX_PER_CONTACT]

    try:
        update_json(_path(contact_id), _default(), update)
    except Exception:
        pass


def record_outbound(contact_id: str, event_id: str, body: str, kind: str = "text") -> None:
    record(contact_id, OUT, event_id=event_id, body=body, kind=kind)


def record_inbound(contact_id: str, event_id: str, body: str, kind: str = "text") -> None:
    record(contact_id, IN, event_id=event_id, body=body, kind=kind, sender=contact_id)


def history(
    contact_id: str,
    *,
    limit: int | None = None,
    dt_from: datetime | None = None,
    dt_to: datetime | None = None,
) -> list:
    """Messages with a contact, oldest first."""
    state = read_json(_path(contact_id), _default())
    messages = [
        item for item in (state.get("messages") or []) if isinstance(item, dict)
    ]
    if dt_from is not None:
        start = dt_from.timestamp()
        messages = [item for item in messages if float(item.get("at") or 0) >= start]
    if dt_to is not None:
        end = dt_to.timestamp()
        messages = [item for item in messages if float(item.get("at") or 0) <= end]
    if limit and limit > 0:
        messages = messages[-limit:]
    return messages


def unanswered(contact_id: str) -> list:
    """Messages I sent since their last reply.

    This is the CLI's notion of "unread": nothing has come back since, so they
    have most likely not read them.
    """
    messages = history(contact_id)
    last_inbound = -1
    for index, item in enumerate(messages):
        if item.get("direction") == IN:
            last_inbound = index
    return [
        item
        for item in messages[last_inbound + 1 :]
        if item.get("direction") == OUT
    ]


def span(contact_id: str, from_id: str, to_id: str = "") -> tuple[list, str]:
    """Messages between two msgids inclusive, oldest first.

    What `iwantto bundle --from X --to Y` names. Returns ``(entries, error)`` —
    an error rather than an exception, because "I cannot find that msgid" is
    something the caller has to say out loud before withdrawing anything.

    ``to_id`` empty means "up to the latest", so bundling everything since a
    message does not require naming its other end.
    """
    from_id = str(from_id or "").strip()
    to_id = str(to_id or "").strip()
    messages = history(contact_id)
    if not messages:
        return [], f"No messages recorded with {contact_id}."

    ids = [str(item.get("event_id") or "") for item in messages]
    try:
        start = ids.index(from_id)
    except ValueError:
        return [], (
            f"--from {from_id} is not a message I have with {contact_id}. "
            "Find the msgid with `iwantto see`."
        )
    if to_id:
        try:
            end = ids.index(to_id)
        except ValueError:
            return [], (
                f"--to {to_id} is not a message I have with {contact_id}. "
                "Find the msgid with `iwantto see`."
            )
    else:
        end = len(messages) - 1
    if end < start:
        return [], "--from comes after --to. Give them oldest first."
    return messages[start : end + 1], ""


def find(event_id: str) -> tuple[str, dict]:
    """Locate a message by its id across every contact. Returns (contact, entry)."""
    event_id = str(event_id or "").strip()
    if not event_id:
        return "", {}
    try:
        names = sorted(os.listdir(MESSAGES_DIR))
    except OSError:
        return "", {}
    for name in names:
        if not name.endswith(".json"):
            continue
        state = read_json(os.path.join(MESSAGES_DIR, name), _default())
        for item in state.get("messages") or []:
            if isinstance(item, dict) and item.get("event_id") == event_id:
                return name[: -len(".json")], item
    return "", {}


def format_entry(entry: dict, *, contact_id: str = "") -> str:
    """One human-readable line, with the msgid a Silicon needs to act on it."""
    direction = "→" if entry.get("direction") == OUT else "←"
    who = "me" if entry.get("direction") == OUT else (contact_id or "them")
    kind = str(entry.get("kind") or "text")
    kind_label = "" if kind == "text" else f" [{kind}]"
    body = " ".join(str(entry.get("body") or "").split())
    msgid = entry.get("event_id") or "(no id)"
    return f"{entry.get('at_iso', '')} {direction} {who}{kind_label} msgid={msgid}\n    {body}"


def format_history(entries: list, *, contact_id: str = "") -> str:
    if not entries:
        return "No messages recorded."
    return "\n".join(format_entry(entry, contact_id=contact_id) for entry in entries)
