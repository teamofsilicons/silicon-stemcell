"""The local contact book, and the one lock that guards it.

Glass owns canonical trust; this file is the last confirmed copy of it plus
everything only this Silicon knows — processed watermarks, room mappings,
downloaded media paths. Every reader and writer goes through
:func:`state_serialized` so a thread and a second process cannot both be
halfway through a write.
"""
from __future__ import annotations

import shutil
import threading
from functools import wraps
from typing import Any

from helpers.state import file_lock, read_json, write_json
from helpers.timefmt import utc_iso as _utc_iso
from interface import constants
from interface.constants import VALID_TRUST_LEVELS

_state_lock = threading.RLock()


def normalize_contact_type(value: Any) -> str:
    """Every contact is a Carbon unless it says otherwise."""
    return "silicon" if "silicon" in str(value or "").lower() else "carbon"


def state_serialized(func):
    @wraps(func)
    def locked(*args, **kwargs):
        with _state_lock, file_lock(constants.CONTACTS_FILE):
            return func(*args, **kwargs)

    return locked




def _default_contacts_state() -> dict[str, Any]:
    return {
        "version": 1,
        "contacts": {},
        "rooms": {},
        "processed_events": {},
        "work_event_refs": {},
        "own_ids": [],
        "last_room_sync": 0,
        "last_seen_event_id": "",
    }


def _migrate_legacy_contacts() -> dict[str, Any] | None:
    if not constants.LEGACY_TELEGRAM_CONTACTS_FILE.exists():
        return None
    legacy = read_json(constants.LEGACY_TELEGRAM_CONTACTS_FILE, {})
    legacy_contacts = legacy.get("contacts") if isinstance(legacy, dict) else None
    if not isinstance(legacy_contacts, dict):
        return None

    state = _default_contacts_state()
    for key, info in legacy_contacts.items():
        if not isinstance(info, dict):
            continue
        contact_type = normalize_contact_type(info.get("contact_type", "carbon"))
        fixed_id = str(info.get("silicon_id") if contact_type == "silicon" else info.get("carbon_id") or key).strip()
        if not fixed_id:
            continue
        state["contacts"][fixed_id] = {
            "contact_type": contact_type,
            "carbon_id": fixed_id if contact_type == "carbon" else "",
            "silicon_id": fixed_id if contact_type == "silicon" else "",
            "fixed_id": fixed_id,
            "room_id": str(info.get("room_id") or ""),
            # Legacy local trust was never authoritative. New Stemcells retain
            # identity metadata only and wait for Glass's confirmed projection.
            "trust_level": "very_low",
            "is_central_carbon": False,
            "local_notes": info.get("local_notes", ""),
            "relation": info.get("relation", ""),
            "description": info.get("description", ""),
            "timezone": info.get("timezone", ""),
            "display_name": info.get("display_name") or info.get("name") or fixed_id,
            "name": info.get("name") or info.get("display_name") or fixed_id,
            "last_processed_event_ids": [],
            "last_processed_event_id": "",
            "last_polled_event_id": "",
            "created_at": _utc_iso(),
            "updated_at": _utc_iso(),
            "metadata": {"migrated_from": "core/telegram/contacts.json"},
        }
        if info.get("room_id"):
            state["rooms"][str(info["room_id"])] = fixed_id
    return state


@state_serialized
def _load_state() -> dict[str, Any]:
    if not constants.CONTACTS_FILE.exists():
        migrated = _migrate_legacy_contacts()
        if migrated:
            _save_state(migrated)
    state = read_json(constants.CONTACTS_FILE, _default_contacts_state())
    state.setdefault("version", 1)
    state.setdefault("contacts", {})
    state.setdefault("rooms", {})
    state.setdefault("processed_events", {})
    state.setdefault("work_event_refs", {})
    state.setdefault("own_ids", [])
    state.setdefault("last_room_sync", 0)
    state.setdefault("last_seen_event_id", "")
    return state


@state_serialized
def _save_state(state: dict[str, Any]) -> None:
    write_json(constants.CONTACTS_FILE, state)


def get_contacts() -> dict[str, Any]:
    return _load_state()


def get_contact(contact_id: str) -> dict[str, Any] | None:
    return _load_state().get("contacts", {}).get(contact_id)


@state_serialized
def apply_glass_trust_policy(entries: dict[str, Any]) -> int:
    """Project a confirmed typed Glass policy onto existing local contacts."""
    if not isinstance(entries, dict):
        return 0
    state = _load_state()
    changed = 0
    now = _utc_iso()
    for fixed_id, contact in state.get("contacts", {}).items():
        if not isinstance(contact, dict):
            continue
        kind = normalize_contact_type(contact.get("contact_type", "carbon"))
        policy = entries.get(f"{kind}:{fixed_id}")
        level = (
            str(policy.get("level") or "")
            if isinstance(policy, dict)
            else "very_low"
        )
        if level not in VALID_TRUST_LEVELS:
            level = "very_low"
        source = (
            str(policy.get("source") or "glass")
            if isinstance(policy, dict)
            else "glass_default"
        )
        is_central_carbon = bool(
            kind == "carbon"
            and isinstance(policy, dict)
            and policy.get("central_carbon")
        )
        contact_changed = False
        if contact.get("trust_level") != level:
            contact["trust_level"] = level
            contact_changed = True
        if contact.get("trust_source") != source:
            contact["trust_source"] = source
            contact_changed = True
        if bool(contact.get("is_central_carbon")) != is_central_carbon:
            contact["is_central_carbon"] = is_central_carbon
            contact_changed = True
        if contact_changed:
            contact["updated_at"] = now
            changed += 1
    if changed:
        _save_state(state)
    return changed


def get_central_contact_id() -> str:
    try:
        from interface.trust import cached_trust_entry
    except Exception:
        return ""
    for contact_id, info in _load_state().get("contacts", {}).items():
        if info.get("contact_type") != "carbon":
            continue
        entry = cached_trust_entry("carbon", contact_id)
        if entry.get("central_carbon"):
            return contact_id
    return ""


@state_serialized
def validate_contacts_integrity() -> bool:
    """Validate fixed-ID contact keys. Restore backup if a local edit corrupts IDs."""
    if not constants.CONTACTS_FILE.exists():
        return True

    state = _load_state()
    bad = False
    for key, info in state.get("contacts", {}).items():
        ctype = info.get("contact_type", "carbon")
        expected = info.get("silicon_id") if ctype == "silicon" else info.get("carbon_id")
        if expected != key:
            print(f"[Interface] WARNING: contact key '{key}' does not match fixed id '{expected}'", flush=True)
            bad = True

    if bad and constants.CONTACTS_BACKUP_FILE.exists():
        shutil.copy2(constants.CONTACTS_BACKUP_FILE, constants.CONTACTS_FILE)
        return False
    if not bad:
        constants.CONTACTS_BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        backup_matches = False
        try:
            backup_matches = (
                constants.CONTACTS_BACKUP_FILE.exists()
                and constants.CONTACTS_FILE.read_bytes()
                == constants.CONTACTS_BACKUP_FILE.read_bytes()
            )
        except OSError:
            backup_matches = False
        if not backup_matches:
            shutil.copy2(constants.CONTACTS_FILE, constants.CONTACTS_BACKUP_FILE)
    return not bad


