"""Who Silicon talks to, and which room reaches them.

Interface owns rooms; this module keeps the local mapping from a fixed contact
id to the direct room that reaches it, and refreshes that mapping from the
server when it goes stale.
"""
from __future__ import annotations

from typing import Any

from helpers.timefmt import now as _now
from helpers.timefmt import utc_iso as _utc_iso
from interface import client as client_module
from interface.constants import ROOM_SYNC_FALLBACK_SECONDS, VALID_TRUST_LEVELS
from interface.errors import InterfaceError
from interface.rpc import _as_list
from interface.state import (
    _load_state,
    _save_state,
    normalize_contact_type,
    state_serialized,
)


def _member_fixed_id(member: dict[str, Any], contact_type: str) -> str:
    if contact_type == "silicon":
        fixed = member.get("silicon_id") or member.get("siliconId") or member.get("username")
    else:
        fixed = member.get("carbon_id") or member.get("carbonId") or member.get("public_id")
    if fixed:
        return str(fixed).strip()
    if "member_kind" in member:
        return ""
    return str(member.get("id") or "").strip()


def _display_name(obj: dict[str, Any], fallback: str) -> str:
    return str(
        obj.get("display_name")
        or obj.get("displayName")
        or obj.get("name")
        or obj.get("username")
        or fallback
    )


def _contact_metadata(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in obj.items()
        if key not in {"members", "events", "content"}
        and isinstance(key, str)
        and not key.startswith("_")
    }


@state_serialized
def upsert_contact(
    contact_type: str,
    fixed_id: str,
    *,
    room_id: str = "",
    display_name: str = "",
    timezone: str = "",
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    contact_type = normalize_contact_type(contact_type)
    fixed_id = str(fixed_id or "").strip()
    if not fixed_id:
        raise ValueError("fixed contact id is required")

    state = _load_state()
    contacts = state.setdefault("contacts", {})
    is_new = fixed_id not in contacts

    if is_new:
        try:
            from interface.trust import cached_trust_entry

            glass_entry = cached_trust_entry(contact_type, fixed_id)
        except Exception:
            glass_entry = {}
        glass_trust = str(glass_entry.get("level") or "very_low")
        contact = {
            "contact_type": contact_type,
            "carbon_id": fixed_id if contact_type == "carbon" else "",
            "silicon_id": fixed_id if contact_type == "silicon" else "",
            "fixed_id": fixed_id,
            "room_id": room_id,
            "trust_level": (
                glass_trust
                if glass_trust in VALID_TRUST_LEVELS
                else "very_low"
            ),
            "trust_source": str(glass_entry.get("source") or "glass_default"),
            "is_central_carbon": bool(
                contact_type == "carbon" and glass_entry.get("central_carbon")
            ),
            "local_notes": "",
            "relation": "",
            "description": "",
            "timezone": timezone or "",
            "display_name": display_name or fixed_id,
            "name": display_name or fixed_id,
            "last_processed_event_ids": [],
            "last_processed_event_id": "",
            "last_polled_event_id": "",
            "created_at": _utc_iso(),
            "updated_at": _utc_iso(),
            "metadata": metadata or {},
        }
        contacts[fixed_id] = contact
    else:
        contact = contacts[fixed_id]
        if contact.get("contact_type") != contact_type:
            raise ValueError(f"Contact id '{fixed_id}' already exists as {contact.get('contact_type')}")
        expected = contact.get("silicon_id") if contact_type == "silicon" else contact.get("carbon_id")
        if expected and expected != fixed_id:
            raise ValueError(f"Contact id '{fixed_id}' is immutable and cannot be remapped from '{expected}'")
        contact.setdefault("fixed_id", fixed_id)
        contact.setdefault("carbon_id", fixed_id if contact_type == "carbon" else "")
        contact.setdefault("silicon_id", fixed_id if contact_type == "silicon" else "")
        contact.setdefault("trust_level", "very_low")
        contact.setdefault("is_central_carbon", False)
        contact.setdefault("local_notes", "")
        contact.setdefault("last_processed_event_ids", [])
        contact.setdefault("metadata", {})
        if room_id:
            contact["room_id"] = room_id
        if display_name:
            contact["display_name"] = display_name
            contact["name"] = display_name
        if timezone:
            contact["timezone"] = timezone
        if metadata:
            contact.setdefault("metadata", {}).update(metadata)
        contact["updated_at"] = _utc_iso()

    if room_id:
        state.setdefault("rooms", {})[room_id] = fixed_id
    _save_state(state)
    return contacts[fixed_id], is_new


def _extract_own_ids(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    ids = []
    for key in (
        "id",
        "carbon_id",
        "carbonId",
        "carbon_username",
        "carbonUsername",
        "silicon_id",
        "siliconId",
        "silicon_username",
        "siliconUsername",
        "username",
        "handle",
        "public_id",
        "publicId",
        "name",
    ):
        value = payload.get(key)
        if value:
            ids.append(str(value))
    for item in _as_list(payload, ("ids", "own_ids", "identities")):
        if isinstance(item, dict):
            ids.extend(_extract_own_ids(item))
        elif item:
            ids.append(str(item))
    return sorted(set(ids))


def _room_id(room: dict[str, Any]) -> str:
    return str(room.get("room_id") or room.get("roomId") or room.get("id") or "").strip()


def _room_is_direct(room: dict[str, Any]) -> bool:
    if "is_direct" in room:
        return bool(room.get("is_direct"))
    if "direct" in room:
        return bool(room.get("direct"))
    if str(room.get("kind") or room.get("type") or "").lower() == "direct":
        return True
    members = room.get("members")
    return isinstance(members, list) and len(members) <= 2


def _other_member(members: list[Any], own_ids: list[str]) -> dict[str, Any] | None:
    for raw in members:
        if not isinstance(raw, dict):
            continue
        if raw.get("is_self") or raw.get("self"):
            continue
        member_ids = set(_extract_own_ids(raw))
        if member_ids and member_ids.intersection(own_ids):
            continue
        return raw
    return None


def _direct_contact_from_room(room: dict[str, Any], members: list[Any], own_ids: list[str]) -> dict[str, Any] | None:
    peers = room.get("peers")
    other = _other_member(peers, own_ids) if isinstance(peers, list) else None
    if other is not None:
        return other

    other = _other_member(members, own_ids) if members else None
    if other is not None:
        return other

    for key in ("contact", "other", "peer", "target", "direct_contact"):
        value = room.get(key)
        if isinstance(value, dict):
            return value

    room_contact_type = normalize_contact_type(room.get("contact_type") or room.get("kind") or room.get("type"))
    candidate_types = [room_contact_type] if room.get("contact_type") or room.get("kind") or room.get("type") else ["carbon", "silicon"]
    for contact_type in candidate_types:
        if contact_type == "silicon":
            fixed_id = str(room.get("silicon_id") or room.get("siliconId") or room.get("username") or "").strip()
        else:
            fixed_id = str(room.get("carbon_id") or room.get("carbonId") or room.get("public_id") or "").strip()
        if fixed_id and fixed_id not in set(own_ids):
            return {
                "contact_type": contact_type,
                "carbon_id": fixed_id if contact_type == "carbon" else "",
                "silicon_id": fixed_id if contact_type == "silicon" else "",
                "display_name": _display_name(room, fixed_id),
            }
    return None


@state_serialized
def _cache_own_ids(own_ids: list[str]) -> list[str]:
    """Merge the latest identity lookup into current state without stale writes."""
    normalized = sorted({str(value) for value in own_ids if value})
    if normalized:
        state = _load_state()
        state["own_ids"] = normalized
        _save_state(state)
    return normalized


def is_own_identity(fixed_id: str) -> bool:
    """Whether this id is us.

    A self-addressed cron is the case that needs it: a reminder is stored in
    Glass against our own identity so it survives a reinstall, and when it fires
    it must land in the session rather than send us to open a DM with ourselves.
    """
    fixed_id = str(fixed_id or "").strip()
    if not fixed_id:
        return False
    state = _load_state()
    if fixed_id in {str(value) for value in (state.get("own_ids") or [])}:
        return True
    profile = state.get("profile")
    if isinstance(profile, dict):
        return fixed_id == str(profile.get("silicon_id") or "").strip()
    return False


@state_serialized
def _finish_room_sync() -> dict[str, Any]:
    """Timestamp room discovery against the newest state written by ingestion."""
    state = _load_state()
    state["last_room_sync"] = _now()
    _save_state(state)
    return state


def discover_rooms(client: client_module.InterfaceClient | None = None, *, force: bool = False) -> dict[str, Any]:
    client = client or client_module.InterfaceClient()
    state = _load_state()
    if (
        not force
        and _now() - float(state.get("last_room_sync") or 0)
        < ROOM_SYNC_FALLBACK_SECONDS
    ):
        return state

    # Asked on every sync we actually perform, not only the first. This used to
    # be gated on `own_ids` being empty, and nothing ever empties it — so after
    # the first success `me_payload` stayed None forever, `_sync_profile_from_glass`
    # returned immediately, and the cached profile (our name, our team, who our
    # central carbon is) could never change. The early return above already
    # rate-limits how often we get here.
    me_payload = None
    own_ids = list(state.get("own_ids") or [])
    try:
        me_payload = client.whoami()
        fresh_ids = _extract_own_ids(me_payload)
        if fresh_ids:
            own_ids = _cache_own_ids(fresh_ids)
    except Exception:
        own_ids = own_ids or _load_state().get("own_ids", [])

    rooms_payload = client.rooms_list()
    rooms = _as_list(rooms_payload, ("rooms", "data", "results"))
    for room in rooms:
        if not isinstance(room, dict) or not _room_is_direct(room):
            continue
        room_id = _room_id(room)
        if not room_id:
            continue

        members = room.get("members") if isinstance(room.get("members"), list) else None
        if members is None:
            try:
                members = _as_list(client.room_members(room_id), ("members", "data", "results"))
            except Exception:
                members = []
        other = _direct_contact_from_room(room, members or [], own_ids)
        if other is None:
            continue

        contact_type = normalize_contact_type(other.get("contact_type") or other.get("kind") or other.get("type"))
        fixed_id = _member_fixed_id(other, contact_type)
        if not fixed_id:
            continue
        upsert_contact(
            contact_type,
            fixed_id,
            room_id=room_id,
            display_name=_display_name(other, fixed_id),
            timezone=str(other.get("timezone") or room.get("timezone") or ""),
            metadata={**_contact_metadata(room), **_contact_metadata(other)},
        )

    # After contacts exist, reconcile the Glass-side profile (description,
    # central carbon) onto them — Glass is the authority on who is central.
    _sync_profile_from_glass(me_payload)

    return _finish_room_sync()


@state_serialized
def _sync_profile_from_glass(payload: Any) -> None:
    """Cache the Silicon's own Glass profile.

    Central-carbon identity is useful profile data, but trust fields are
    projected exclusively by the revisioned Glass trust-policy endpoint.
    """
    if not isinstance(payload, dict):
        return
    state = _load_state()
    existing = state.get("profile")
    profile = dict(existing) if isinstance(existing, dict) else {}
    for key in (
        "silicon_id",
        "name",
        "tagline",
        "description",
        "architecture_node_id",
        "job_description",
        "advertising_memory_path",
    ):
        if key in payload:
            profile[key] = str(payload.get(key) or "")

    team_keys = ("owner_team_slug", "team_slug", "team")
    if any(key in payload for key in team_keys):
        team_slug = next(
            (
                str(payload.get(key) or "")
                for key in team_keys
                if key in payload
            ),
            "",
        )
        profile["owner_team_slug"] = team_slug
        profile["team"] = team_slug

    central_raw = payload.get("central_carbon")
    if "central_carbon" in payload:
        profile["central_carbon"] = (
            central_raw if isinstance(central_raw, dict) else None
        )
    state["profile"] = profile
    _save_state(state)


def get_own_profile() -> dict[str, Any]:
    """The silicon's cached Glass identity, role, team, and central carbon.

    The cache is refreshed on every room sync.
    """
    profile = _load_state().get("profile")
    return profile if isinstance(profile, dict) else {}


def ensure_contact_for_target(contact_type: str, fixed_id: str, client: client_module.InterfaceClient | None = None) -> dict[str, Any]:
    contact_type = normalize_contact_type(contact_type)
    fixed_id = str(fixed_id or "").strip()
    if not fixed_id:
        raise ValueError("target fixed id is required")

    state = _load_state()
    contact = state.get("contacts", {}).get(fixed_id)
    if contact and contact.get("room_id"):
        return contact

    client = client or client_module.InterfaceClient()
    room_id = ""
    try:
        payload = client.ensure_direct_room(contact_type, fixed_id)
        if isinstance(payload, dict):
            room_id = str(payload.get("room_id") or payload.get("roomId") or payload.get("id") or "")
    except InterfaceError as exc:
        raise InterfaceError(f"Could not open DM with {contact_type} '{fixed_id}': {exc}") from exc
    except Exception as exc:
        raise InterfaceError(f"Could not open DM with {contact_type} '{fixed_id}': {exc}") from exc

    if not room_id:
        raise InterfaceError(f"Could not open DM with {contact_type} '{fixed_id}': no DM id returned")

    try:
        contact, _ = upsert_contact(contact_type, fixed_id, room_id=room_id, display_name=fixed_id)
        return contact
    except Exception as exc:
        raise InterfaceError(f"Could not save DM contact for {contact_type} '{fixed_id}': {exc}") from exc


def _contact_for_room(room_id: str, client: client_module.InterfaceClient | None = None) -> tuple[str, dict[str, Any] | None, bool]:
    state = _load_state()
    contact_id = state.get("rooms", {}).get(room_id)
    if contact_id:
        return contact_id, state.get("contacts", {}).get(contact_id), False

    if client:
        try:
            discover_rooms(client, force=True)
        except Exception:
            pass
        state = _load_state()
        contact_id = state.get("rooms", {}).get(room_id)
        if contact_id:
            return contact_id, state.get("contacts", {}).get(contact_id), False

    return "", None, False


