"""Working out who a target is, and how a message reaches them.

Two problems live here.

**Typing a bare id.** The CLI is written as `iwantto send shubham`, not
`iwantto send --carbon shubham`. So a bare name has to be resolved to a typed
identity: local contacts first, then the Glass trust policy (the only
authoritative typed directory this Silicon holds), matching on id before
display name.

**Choosing the path.** A manager talking to its *own* contact sends directly.
Anyone else is reached through their manager, because a contact has exactly one
manager and everything said to them should pass through it — otherwise two
managers ask the same carbon the same question twice.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.state import update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
ROUTING_FILE = os.path.join(
    os.fspath(STATE_DIR), "iwantto_routing.json"
)

# Attached the first time this Silicon speaks to a contact it has never spoken
# to, so the freshly started manager understands why it exists.
FIRST_CONTACT_NOTE = (
    "you are not yet talking to your carbon, but this is the message that "
    "{sender} wants to send to your carbon, its advised to pass it forward."
)


class RoutingError(RuntimeError):
    """A target could not be resolved to exactly one typed identity."""


@dataclass(frozen=True)
class Target:
    """A resolved destination."""

    kind: str  # "carbon" | "silicon"
    fixed_id: str
    display_name: str = ""
    known: bool = False  # already a local contact

    @property
    def label(self) -> str:
        name = self.display_name or self.fixed_id
        return f"{name} ({self.fixed_id})" if name != self.fixed_id else name


def _local_contacts() -> dict:
    try:
        from interface.adapter import get_contacts

        contacts = get_contacts()
    except Exception:
        return {}
    return contacts if isinstance(contacts, dict) else {}


def _trust_directory() -> list:
    """Every typed identity Glass has confirmed for this Silicon."""
    try:
        from interface.trust import confirmed_trust_policy_snapshot

        policy = confirmed_trust_policy_snapshot(root=PROJECT_ROOT)
    except Exception:
        return []
    entries = policy.get("entries") if isinstance(policy, dict) else None
    return [entry for entry in (entries or []) if isinstance(entry, dict)]


def resolve_target(name: str, *, kind_hint: str = "") -> Target:
    """Resolve a bare id or display name to one typed identity.

    ``kind_hint`` comes from an explicit ``--carbon``/``--silicon`` flag and
    settles a name that would otherwise be ambiguous.
    """
    raw = str(name or "").strip().lstrip("@")
    if not raw:
        raise RoutingError("A carbon id or silicon id is required.")
    kind_hint = str(kind_hint or "").strip().lower()

    contacts = _local_contacts()
    contact = contacts.get(raw)
    if isinstance(contact, dict):
        kind = str(contact.get("contact_type") or "carbon")
        if not kind_hint or kind_hint == kind:
            return Target(
                kind=kind,
                fixed_id=raw,
                display_name=str(contact.get("display_name") or ""),
                known=True,
            )

    lowered = raw.lower()
    by_name = [
        (contact_id, contact)
        for contact_id, contact in contacts.items()
        if isinstance(contact, dict)
        and str(contact.get("display_name") or "").strip().lower() == lowered
        and (
            not kind_hint
            or str(contact.get("contact_type") or "carbon") == kind_hint
        )
    ]
    if len(by_name) == 1:
        contact_id, contact = by_name[0]
        return Target(
            kind=str(contact.get("contact_type") or "carbon"),
            fixed_id=contact_id,
            display_name=str(contact.get("display_name") or ""),
            known=True,
        )
    if len(by_name) > 1:
        names = ", ".join(sorted(contact_id for contact_id, _ in by_name))
        raise RoutingError(
            f"'{raw}' matches more than one contact ({names}). "
            "Use the exact id."
        )

    directory = _trust_directory()
    matches = [
        entry
        for entry in directory
        if str(entry.get("id") or "") == raw
        and (not kind_hint or str(entry.get("kind") or "") == kind_hint)
    ] or [
        entry
        for entry in directory
        if str(entry.get("name") or "").strip().lower() == lowered
        and (not kind_hint or str(entry.get("kind") or "") == kind_hint)
    ]
    if len(matches) == 1:
        entry = matches[0]
        return Target(
            kind=str(entry.get("kind") or "carbon"),
            fixed_id=str(entry.get("id") or raw),
            display_name=str(entry.get("name") or ""),
            known=False,
        )
    if len(matches) > 1:
        options = ", ".join(
            sorted(f"{entry.get('kind')}:{entry.get('id')}" for entry in matches)
        )
        raise RoutingError(
            f"'{raw}' matches more than one identity ({options}). "
            "Use the exact id, or pass --carbon / --silicon."
        )

    if kind_hint:
        return Target(kind=kind_hint, fixed_id=raw, known=False)
    raise RoutingError(
        f"I don't know who '{raw}' is. Nobody by that id or name is in my "
        "contacts or my Glass trust policy. If they are real and new, say "
        f"which they are: --carbon {raw} or --silicon {raw}."
    )


def _default_routing_state() -> dict:
    return {"version": 1, "first_contact": {}}


def claim_first_contact(fixed_id: str) -> bool:
    """True exactly once per target: on the first message this Silicon sends it.

    Recorded durably so a retried or repeated send does not re-attach the
    first-contact note to a manager that has already been told why it exists.
    """
    fixed_id = str(fixed_id or "")
    if not fixed_id:
        return False

    def update(state):
        seen = state.setdefault("first_contact", {})
        if fixed_id in seen:
            return False
        seen[fixed_id] = time.time()
        return True

    try:
        return bool(update_json(ROUTING_FILE, _default_routing_state(), update))
    except Exception:
        return False


def is_new_relationship(target: Target) -> bool:
    """Has this Silicon ever actually exchanged a message with the target?

    A contact row can exist because Glass listed them without a single message
    ever passing, so presence alone does not count as a relationship.
    """
    if not target.known:
        return True
    contact = _local_contacts().get(target.fixed_id)
    if not isinstance(contact, dict):
        return True
    return not (
        contact.get("last_processed_event_id")
        or contact.get("last_processed_event_ids")
        or contact.get("last_polled_event_id")
    )


def first_contact_preamble(sender_label: str) -> str:
    return FIRST_CONTACT_NOTE.format(sender=sender_label)
