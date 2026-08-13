"""The one Silicon session, and who it is currently answering.

There used to be a manager per contact: one for your Carbon, and one more for
every Silicon you could talk to. A message to a peer Silicon crossed four
sessions before any work started — your manager, your manager-for-them, their
manager-for-you, their manager. Now there is one session, :data:`SILICON`, and
every message from everyone lands in it.

Two things follow from that, and both live here.

**A message needs to say who it is from.** One session reading messages from
five people can only tell them apart if each one is labelled. :func:`envelope`
is that label, and it is the only place the format is written.

**A frame needs to know which room it belongs in.** A reply names its target,
but a typing indicator or a work card does not — it belongs to whoever is being
answered right now. :func:`answering` records that for the duration of a turn,
and anything addressed to :data:`SILICON` fans out to it. Since the envelope is
generated here, :func:`origins_in` can read it back, so the format and its
parser cannot drift apart.
"""
from __future__ import annotations

import datetime
import re
import threading
import time
from contextlib import contextmanager

# Not "silicon": `contacts.json` is keyed by bare id, so a Carbon called
# "silicon" would otherwise collide with the session itself.
SILICON = "__silicon__"

_LOCK = threading.RLock()
_ORIGINS: list[str] = []

_TYPED_ID = r"\((carbon|silicon): ([^)\s]+)\)"
# Two markers name a contact: a message they sent, and work standing in their
# name that nobody sent — a cron firing. Both are generated below, so the
# pattern and the text it reads can only be changed together.
_ENVELOPE_RE = re.compile(
    rf"^(?:message from @.*? |\[for @.*? ){_TYPED_ID}",
    re.MULTILINE,
)


def envelope(
    contact_id: str,
    *,
    display_name: str = "",
    contact_type: str = "carbon",
    trust: str = "very_low",
    at: float | None = None,
) -> str:
    """The header every inbound message carries, whoever it came from.

    Both lines are for the session to read, but the first is also how the
    runtime recovers the sender — keep it in step with :data:`_ENVELOPE_RE`.
    """
    kind = "silicon" if str(contact_type or "") == "silicon" else "carbon"
    name = str(display_name or contact_id or "unknown").strip() or "unknown"
    return (
        f"message from @{name} ({kind}: {contact_id}) "
        f"(trust: {trust or 'very_low'})\n"
        f"{readable_time(at)}:"
    )


def readable_time(at: float | None = None) -> str:
    """An instant as a person reads it, in this machine's timezone.

    `%-I` would drop the leading zero in one character but is not portable, so
    the hour is trimmed by hand.
    """
    moment = datetime.datetime.fromtimestamp(
        time.time() if at is None else at
    ).astimezone()
    return (
        f"{moment.strftime('%d %b %Y')}, {moment.hour % 12 or 12}"
        f"{moment.strftime(':%M %p %Z')}"
    )


def regarding(
    contact_id: str,
    *,
    display_name: str = "",
    contact_type: str = "carbon",
) -> str:
    """Whose behalf a root is on, when nobody actually sent it.

    A cron fires for somebody without them saying anything. There is no sender
    and no trust to apply, but there is still a room the work belongs in.
    """
    kind = "silicon" if str(contact_type or "") == "silicon" else "carbon"
    name = str(display_name or contact_id or "unknown").strip() or "unknown"
    return f"[for @{name} ({kind}: {contact_id})]"


def origins_in(context: str) -> list[str]:
    """Every contact this context names, in order, deduped."""
    seen: list[str] = []
    for _kind, contact_id in _ENVELOPE_RE.findall(str(context or "")):
        if contact_id not in seen:
            seen.append(contact_id)
    return seen


@contextmanager
def answering(contact_ids):
    """Record who this turn is for, so untargeted frames know their rooms.

    Nested and re-entrant: a turn adds the batch it is answering to whatever an
    outer turn was already answering. On the way out the whole list is restored
    to what it was, so an origin added mid-turn by :func:`also_answering` leaves
    with the turn that absorbed it.
    """
    with _LOCK:
        restore = list(_ORIGINS)
    also_answering(contact_ids)
    try:
        yield live_origins()
    finally:
        with _LOCK:
            _ORIGINS[:] = restore


def also_answering(contact_ids) -> None:
    """Add origins to the live turn — a message that arrived while it ran.

    It stays for the rest of the turn that absorbed it, which is exactly the
    turn that will produce frames about it.
    """
    with _LOCK:
        for contact_id in contact_ids or ():
            contact_id = str(contact_id or "")
            if contact_id and contact_id != SILICON and contact_id not in _ORIGINS:
                _ORIGINS.append(contact_id)


def live_origins() -> tuple:
    """Who the session is answering right now."""
    with _LOCK:
        return tuple(_ORIGINS)


def primary_origin() -> str:
    """The first contact of the live turn, for anything that needs exactly one."""
    with _LOCK:
        return _ORIGINS[0] if _ORIGINS else ""


def resolve_rooms(contact_id: str) -> tuple:
    """Which contacts a frame addressed to ``contact_id`` should reach.

    A real contact is itself. :data:`SILICON` is not a room, so it fans out to
    whoever the live turn is answering.
    """
    contact_id = str(contact_id or "")
    if contact_id and contact_id != SILICON:
        return (contact_id,)
    return live_origins()
