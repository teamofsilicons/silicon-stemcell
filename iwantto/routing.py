"""Working out who a target is.

One problem lives here now. The CLI is written as `iwantto send shubham`, not
`iwantto send --carbon shubham`, so a bare name has to be resolved to a typed
identity: local contacts first, then the Glass trust policy (the only
authoritative typed directory this Silicon holds), matching on id before display
name.

Choosing a path used to live here too. It does not need to: there is one session
and one hop, so a resolved target *is* the route.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from helpers.paths import DATA_ROOT

PROJECT_ROOT = os.fspath(DATA_ROOT)


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
        from interface import get_contacts

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
