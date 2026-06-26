"""Silicon Interface transport, local contacts, and event ingestion.

Interface and Glass own the wire. Stemcell owns local contact trust, manager
state, processed watermarks, and downloaded media paths.
"""
from __future__ import annotations

import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / "core" / "interface_state"
CONTACTS_FILE = STATE_DIR / "contacts.json"
CONTACTS_BACKUP_FILE = STATE_DIR / "contacts_backup.json"
MEDIA_DIR = STATE_DIR / "media"
LEGACY_TELEGRAM_CONTACTS_FILE = PROJECT_ROOT / "core" / "telegram" / "contacts.json"

VALID_TRUST_LEVELS = ["very_low", "low", "ok", "high", "very_high", "ultimate"]
USER_VISIBLE_EVENT_TYPES = {"m.text", "m.image", "m.file", "m.voice", "m.tts"}
IGNORED_EVENT_TYPES = {"m.progress", "m.reaction", "m.session_marker", "m.system"}
RICH_MEDIA_RE = re.compile(r"\[(file|voice)=((?:[^\[\]]|\[[^\]]*\])*)\]", re.DOTALL)
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

_listener_thread: threading.Thread | None = None
_listener_lock = threading.Lock()
_listener_stop: threading.Event | None = None
_listener_proc: subprocess.Popen | None = None
_event_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
_last_listener_error = 0.0
_event_sync_lock = threading.Lock()
_boot_event_sync_done = False

EVENT_SYNC_LIMIT = 500
EVENT_SYNC_MAX_PAGES = 5
SAFETY_EVENT_SYNC_SECONDS = 300
SAFETY_EVENT_SYNC_JITTER_SECONDS = 120


class InterfaceError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


def _utc_iso(ts: float | None = None) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts or _now(), tz=timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _default_contacts_state() -> dict[str, Any]:
    return {
        "version": 1,
        "contacts": {},
        "rooms": {},
        "processed_events": {},
        "own_ids": [],
        "last_room_sync": 0,
        "last_event_cursor": "",
        "last_event_sync": 0,
        "next_safety_event_sync": 0,
    }


def _migrate_legacy_contacts() -> dict[str, Any] | None:
    if not LEGACY_TELEGRAM_CONTACTS_FILE.exists():
        return None
    legacy = _read_json(LEGACY_TELEGRAM_CONTACTS_FILE, {})
    legacy_contacts = legacy.get("contacts") if isinstance(legacy, dict) else None
    if not isinstance(legacy_contacts, dict):
        return None

    state = _default_contacts_state()
    for key, info in legacy_contacts.items():
        if not isinstance(info, dict):
            continue
        contact_type = _normalize_contact_type(info.get("contact_type", "carbon"))
        fixed_id = str(info.get("silicon_id") if contact_type == "silicon" else info.get("carbon_id") or key).strip()
        if not fixed_id:
            continue
        state["contacts"][fixed_id] = {
            "contact_type": contact_type,
            "carbon_id": fixed_id if contact_type == "carbon" else "",
            "silicon_id": fixed_id if contact_type == "silicon" else "",
            "fixed_id": fixed_id,
            "room_id": str(info.get("room_id") or ""),
            "trust_level": info.get("trust_level", "very_low"),
            "is_central_carbon": bool(info.get("is_central_carbon", False)),
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


def _load_state() -> dict[str, Any]:
    if not CONTACTS_FILE.exists():
        migrated = _migrate_legacy_contacts()
        if migrated:
            _save_state(migrated)
    state = _read_json(CONTACTS_FILE, _default_contacts_state())
    state.setdefault("version", 1)
    state.setdefault("contacts", {})
    state.setdefault("rooms", {})
    state.setdefault("processed_events", {})
    state.setdefault("own_ids", [])
    state.setdefault("last_room_sync", 0)
    state.setdefault("last_event_cursor", "")
    state.setdefault("last_event_sync", 0)
    state.setdefault("next_safety_event_sync", 0)
    return state


def _save_state(state: dict[str, Any]) -> None:
    _write_json(CONTACTS_FILE, state)


def get_contacts() -> dict[str, Any]:
    return _load_state()


def get_contact(contact_id: str) -> dict[str, Any] | None:
    return _load_state().get("contacts", {}).get(contact_id)


def get_central_contact_id() -> str:
    for contact_id, info in _load_state().get("contacts", {}).items():
        if info.get("contact_type") == "carbon" and info.get("is_central_carbon"):
            return contact_id
    return ""


def validate_contacts_integrity() -> bool:
    """Validate fixed-ID contact keys. Restore backup if a local edit corrupts IDs."""
    if not CONTACTS_FILE.exists():
        return True

    state = _load_state()
    bad = False
    for key, info in state.get("contacts", {}).items():
        ctype = info.get("contact_type", "carbon")
        expected = info.get("silicon_id") if ctype == "silicon" else info.get("carbon_id")
        if expected != key:
            print(f"[Interface] WARNING: contact key '{key}' does not match fixed id '{expected}'", flush=True)
            bad = True

    if bad and CONTACTS_BACKUP_FILE.exists():
        shutil.copy2(CONTACTS_BACKUP_FILE, CONTACTS_FILE)
        return False
    if not bad:
        CONTACTS_BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONTACTS_FILE, CONTACTS_BACKUP_FILE)
    return not bad


def _as_list(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _parse_json_output(stdout: str) -> Any:
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
    return {"text": text}


class InterfaceClient:
    """Small adapter around `si --json`.

    Command methods intentionally keep a thin shape. The CLI is the protocol
    adapter; stemcell only normalizes JSON and builds stable calls.
    """

    def __init__(self, executable: str | None = None, cwd: Path | None = None):
        self.executable = executable
        self.cwd = Path(cwd or PROJECT_ROOT)

    def _candidates(self) -> list[str]:
        if self.executable:
            return [self.executable]
        local = self.cwd / ".silicon-interface" / "bin" / "si"
        return [str(local), "si", "silicon-interface"]

    def _resolve_executable(self) -> str:
        for candidate in self._candidates():
            if os.path.sep in candidate:
                if Path(candidate).exists():
                    return candidate
            elif shutil.which(candidate):
                return candidate
        raise InterfaceError("Silicon Interface CLI not found. Expected ./.silicon-interface/bin/si, si, or silicon-interface.")

    def base_cmd(self, json_mode: bool = True) -> list[str]:
        cmd = [self._resolve_executable()]
        if json_mode:
            cmd.append("--json")
        return cmd

    def run(
        self,
        args: list[str],
        *,
        json_mode: bool = True,
        input_text: str | None = None,
        timeout: int = 60,
        check: bool = True,
    ) -> Any:
        cmd = self.base_cmd(json_mode=json_mode) + [str(arg) for arg in args if arg is not None]
        proc = subprocess.run(
            cmd,
            input=input_text,
            cwd=str(self.cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise InterfaceError(detail or f"Interface command failed: {' '.join(cmd)}")
        if json_mode:
            return _parse_json_output(proc.stdout)
        return proc.stdout

    def popen(self, args: list[str], *, json_mode: bool = True) -> subprocess.Popen:
        cmd = self.base_cmd(json_mode=json_mode) + [str(arg) for arg in args if arg is not None]
        return subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def whoami(self) -> Any:
        return self.run(["me"], timeout=30)

    def rooms_list(self) -> Any:
        return self.run(["rooms", "list"], timeout=30)

    def room_members(self, room_id: str) -> Any:
        payload = self.run(["rooms", "show", room_id, "--limit", "0"], timeout=45)
        if isinstance(payload, dict) and "members" in payload:
            return payload.get("members") or []
        return payload

    def ensure_direct_room(self, contact_type: str, fixed_id: str) -> Any:
        return self.run(["rooms", "direct", contact_type, fixed_id], timeout=60)

    def events_list(self, room_id: str, since: str = "") -> Any:
        args = ["messages", "list", room_id, "--limit", "200"]
        return self.run(args, timeout=45)

    def events_sync(self, after: str = "", limit: int = EVENT_SYNC_LIMIT) -> Any:
        args = ["events", "sync", "--limit", str(limit), "--no-cursor"]
        if after:
            args.extend(["--after", after])
        return self.run(args, timeout=60)

    def listen_all_process(self) -> subprocess.Popen:
        return self.popen(["listen", "all", "--once", "--no-sync"])

    def send(self, room_id: str, message: str) -> Any:
        return self.run(["send", room_id, message], timeout=60)

    def send_file(self, room_id: str, path: str) -> Any:
        return self.run(["send-file", room_id, path], timeout=120)

    def tts(self, room_id: str, text: str) -> Any:
        return self.run(["tts", "--room", room_id, text], timeout=180)

    def read(self, room_id: str, event_id: str) -> Any:
        return self.run(["read", room_id, event_id], timeout=30, check=False)

    def media_show(self, media_id: str) -> Any:
        return self.run(["media", "show", media_id], timeout=30)

    def stt(self, value: str) -> Any:
        return self.run(["stt", value], timeout=180)

    def progress(self, room_id: str, group: str, state: str, message: str = "") -> Any:
        args = ["progress", room_id, state, "--group", group]
        if message:
            args.extend(["--note", message])
        return self.run(args, timeout=30, check=False)

    def remote_browser(self, room_id: str, url: str, ttl_minutes: int) -> Any:
        return self.run(["remote-browser", room_id, url, "--ttl-minutes", str(ttl_minutes)], timeout=30)

    def take_back_complete(self, request_id: str, replacement: str) -> Any:
        return self.run(["take-back", "complete", request_id, replacement], timeout=60)

    def take_back_event(self, event_id: str, reason: str = "", force: bool = False) -> Any:
        args = ["take-back", event_id]
        if reason:
            args.extend(["--reason", reason])
        if force:
            args.append("--force")
        return self.run(args, timeout=60)

    def crons_list(self) -> Any:
        return self.run(["crons", "list", "--mine"], timeout=45)

    def cron_create(self, trigger: str, task: str, targets: list[dict[str, Any]]) -> Any:
        # The Interface CLI takes recipients as repeated `--target kind:id` flags
        # (kind ∈ carbon|silicon), NOT a single `--targets` JSON blob — passing
        # JSON makes it fail with "Pass at least one --target kind:id."
        args = ["crons", "create", "--trigger", trigger, "--task", task]
        for t in targets:
            kind = str(t.get("kind") or "").strip().lower()
            ident = str(
                t.get("id") or t.get("carbon_id") or t.get("silicon_id") or ""
            ).strip()
            if not kind:
                kind = "carbon" if t.get("carbon_id") else "silicon" if t.get("silicon_id") else ""
            if kind and ident:
                args.extend(["--target", f"{kind}:{ident}"])
        return self.run(args, timeout=60)

    def cron_update(self, cron_id: str, **updates: Any) -> Any:
        args = ["crons", "update", cron_id]
        for key in ("trigger", "task", "active"):
            if key in updates and updates[key] is not None:
                args.extend([f"--{key}", str(updates[key]).lower() if isinstance(updates[key], bool) else str(updates[key])])
        return self.run(args, timeout=60)

    def cron_delete(self, cron_id: str) -> Any:
        return self.run(["crons", "delete", cron_id], timeout=60)


def _normalize_contact_type(value: Any) -> str:
    value = str(value or "").lower()
    if "silicon" in value:
        return "silicon"
    return "carbon"


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


def upsert_contact(
    contact_type: str,
    fixed_id: str,
    *,
    room_id: str = "",
    display_name: str = "",
    timezone: str = "",
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    contact_type = _normalize_contact_type(contact_type)
    fixed_id = str(fixed_id or "").strip()
    if not fixed_id:
        raise ValueError("fixed contact id is required")

    state = _load_state()
    contacts = state.setdefault("contacts", {})
    is_new = fixed_id not in contacts

    if is_new:
        first_carbon = contact_type == "carbon" and not any(
            c.get("contact_type") == "carbon" and c.get("is_central_carbon")
            for c in contacts.values()
        )
        contact = {
            "contact_type": contact_type,
            "carbon_id": fixed_id if contact_type == "carbon" else "",
            "silicon_id": fixed_id if contact_type == "silicon" else "",
            "fixed_id": fixed_id,
            "room_id": room_id,
            "trust_level": "ultimate" if first_carbon else "very_low",
            "is_central_carbon": bool(first_carbon),
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

    room_contact_type = _normalize_contact_type(room.get("contact_type") or room.get("kind") or room.get("type"))
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


def _rooms_signature(state: dict[str, Any]) -> str:
    rooms = state.get("rooms", {})
    pairs = sorted((str(room_id), str(contact_id)) for room_id, contact_id in rooms.items())
    return json.dumps(pairs, separators=(",", ":"))


def discover_rooms(client: InterfaceClient | None = None, *, force: bool = False) -> dict[str, Any]:
    client = client or InterfaceClient()
    state = _load_state()
    before_signature = _rooms_signature(state)
    if not force and _now() - float(state.get("last_room_sync") or 0) < 60:
        return state

    me_payload = None
    try:
        me_payload = client.whoami()
        own_ids = _extract_own_ids(me_payload)
        if own_ids:
            state["own_ids"] = own_ids
            _save_state(state)
    except Exception:
        own_ids = state.get("own_ids", [])

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

        contact_type = _normalize_contact_type(other.get("contact_type") or other.get("kind") or other.get("type"))
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

    state = _load_state()
    state["last_room_sync"] = _now()
    _save_state(state)
    if before_signature != _rooms_signature(state) and _listener_thread and _listener_thread.is_alive():
        restart_listener()
    return state


def _sync_profile_from_glass(payload: Any) -> None:
    """Cache the silicon's own Glass profile and reconcile the central carbon.

    Glass claims the central carbon when the first non-lord carbon actually
    messages the silicon — lords (platform setup) never claim. When the
    whoami payload carries a `central_carbon` key, it overrides the local
    first-contact bootstrap: the matching carbon contact is flagged (and
    raised to ultimate trust on a fresh claim), every other carbon is
    unflagged. An absent key (older Glass) leaves local state untouched.
    """
    if not isinstance(payload, dict):
        return
    state = _load_state()
    central_raw = payload.get("central_carbon")
    state["profile"] = {
        "name": str(payload.get("name") or ""),
        "tagline": str(payload.get("tagline") or ""),
        "description": str(payload.get("description") or ""),
        "central_carbon": central_raw if isinstance(central_raw, dict) else None,
    }
    if "central_carbon" in payload:
        central_id = str((central_raw or {}).get("carbon_id") or "").strip()
        for fixed_id, contact in state.get("contacts", {}).items():
            if contact.get("contact_type") != "carbon":
                continue
            should_be_central = bool(central_id) and fixed_id == central_id
            if should_be_central and not contact.get("is_central_carbon"):
                contact["is_central_carbon"] = True
                contact["trust_level"] = "ultimate"
                contact["updated_at"] = _utc_iso()
            elif not should_be_central and contact.get("is_central_carbon"):
                # Trust is local/user-managed — only the flag is withdrawn.
                contact["is_central_carbon"] = False
                contact["updated_at"] = _utc_iso()
    _save_state(state)


def get_own_profile() -> dict[str, Any]:
    """The silicon's cached Glass profile (name, tagline, description,
    central_carbon) — refreshed on every room sync."""
    profile = _load_state().get("profile")
    return profile if isinstance(profile, dict) else {}


def ensure_contact_for_target(contact_type: str, fixed_id: str, client: InterfaceClient | None = None) -> dict[str, Any]:
    contact_type = _normalize_contact_type(contact_type)
    fixed_id = str(fixed_id or "").strip()
    if not fixed_id:
        raise ValueError("target fixed id is required")

    state = _load_state()
    contact = state.get("contacts", {}).get(fixed_id)
    if contact and contact.get("room_id"):
        return contact

    client = client or InterfaceClient()
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


def _contact_for_room(room_id: str, client: InterfaceClient | None = None) -> tuple[str, dict[str, Any] | None, bool]:
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


def _event_content(event: dict[str, Any]) -> dict[str, Any]:
    content = event.get("content")
    return content if isinstance(content, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


def _event_type(event: dict[str, Any]) -> str:
    content = _event_content(event)
    return _first_text(event.get("type"), event.get("event_type"), event.get("eventType"), content.get("msgtype"), content.get("type"))


def _event_id(event: dict[str, Any]) -> str:
    content = _event_content(event)
    return _first_text(event.get("event_id"), event.get("eventId"), event.get("id"), content.get("event_id"), content.get("id"))


def _event_room_id(event: dict[str, Any]) -> str:
    content = _event_content(event)
    room_id = _first_text(event.get("room_id"), event.get("roomId"), content.get("room_id"))
    if room_id:
        return room_id
    room = event.get("room")
    return room if isinstance(room, str) else ""


def _event_sender(event: dict[str, Any]) -> str:
    candidates = _event_sender_candidates(event)
    return candidates[0] if candidates else ""


def _event_sender_candidates(event: dict[str, Any]) -> list[str]:
    content = _event_content(event)
    values: list[Any] = []
    sender = event.get("sender")
    if isinstance(sender, dict):
        values.extend(
            [
                sender.get("id"),
                sender.get("carbon_id"),
                sender.get("carbonId"),
                sender.get("silicon_id"),
                sender.get("siliconId"),
                sender.get("username"),
                sender.get("handle"),
                sender.get("public_id"),
                sender.get("publicId"),
                sender.get("name"),
            ]
        )
    else:
        values.append(sender)

    values.extend(
        [
            event.get("sender_id"),
            event.get("senderId"),
            event.get("sender_handle"),
            event.get("senderHandle"),
            event.get("sender_username"),
            event.get("senderUsername"),
            event.get("sender_public_id"),
            event.get("senderPublicId"),
            event.get("carbon_id"),
            event.get("carbonId"),
            event.get("silicon_id"),
            event.get("siliconId"),
            content.get("sender"),
            content.get("sender_id"),
            content.get("senderId"),
            content.get("sender_handle"),
            content.get("senderHandle"),
            content.get("sender_username"),
            content.get("senderUsername"),
            content.get("carbon_id"),
            content.get("carbonId"),
            content.get("silicon_id"),
            content.get("siliconId"),
        ]
    )
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _identity_set(values: Any) -> set[str]:
    out: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        out.add(text)
        out.add(text.lower())
    return out


def _event_is_self(event: dict[str, Any], state: dict[str, Any]) -> bool:
    if event.get("is_self") or event.get("self") or event.get("sender_is_self"):
        return True
    own_ids = _identity_set(state.get("own_ids") or [])
    if not own_ids:
        return False
    senders = _identity_set(_event_sender_candidates(event))
    return bool(senders and senders.intersection(own_ids))


def _event_body(event: dict[str, Any]) -> str:
    content = _event_content(event)
    return _first_text(
        event.get("body"),
        event.get("text"),
        event.get("message"),
        event.get("caption"),
        content.get("body"),
        content.get("text"),
        content.get("message"),
        content.get("caption"),
    ).strip()


def _event_display_time(event: dict[str, Any]) -> str:
    content = _event_content(event)
    return _first_text(event.get("display_time"), event.get("displayTime"), content.get("display_time"), event.get("created_at"), event.get("createdAt"))


def _event_media_id(event: dict[str, Any]) -> str:
    content = _event_content(event)
    for obj in (event, content, event.get("file"), event.get("attachment"), content.get("file"), content.get("attachment")):
        if isinstance(obj, dict):
            value = _first_text(obj.get("media_id"), obj.get("mediaId"), obj.get("id"))
            if value:
                return value
    return ""


def _event_filename(event: dict[str, Any], media_id: str) -> str:
    content = _event_content(event)
    for obj in (event, content, event.get("file"), event.get("attachment"), content.get("file"), content.get("attachment")):
        if isinstance(obj, dict):
            value = _first_text(obj.get("filename"), obj.get("file_name"), obj.get("name"))
            if value:
                return Path(value).name
    return f"{media_id or 'media'}"


def _event_reply_to(event: dict[str, Any]) -> str:
    content = _event_content(event)
    for obj in (event, content):
        value = _first_text(obj.get("reply_to"), obj.get("reply_to_event_id"), obj.get("replyToEventId"))
        if value:
            return value
        reply = obj.get("reply")
        if isinstance(reply, dict):
            value = _first_text(reply.get("event_id"), reply.get("eventId"), reply.get("id"), reply.get("body"), reply.get("text"))
            if value:
                return value
    relates_to = content.get("m.relates_to") or content.get("relates_to")
    if isinstance(relates_to, dict):
        return _first_text(relates_to.get("m.in_reply_to", {}).get("event_id") if isinstance(relates_to.get("m.in_reply_to"), dict) else "", relates_to.get("event_id"))
    return ""


def _event_take_back_request_id(event: dict[str, Any]) -> str:
    content = _event_content(event)
    for obj in (event, content):
        value = _first_text(obj.get("take_back_request_id"), obj.get("takeBackRequestId"), obj.get("take_back_id"))
        if value:
            return value
        take_back = obj.get("take_back") or obj.get("takeBack")
        if isinstance(take_back, dict):
            value = _first_text(take_back.get("request_id"), take_back.get("requestId"), take_back.get("id"))
            if value:
                return value
    return ""


def _remember_processed(contact_id: str, event_id: str, room_id: str = "") -> None:
    if not event_id:
        return
    state = _load_state()
    _advance_event_cursor(state, event_id)
    contact = state.setdefault("contacts", {}).get(contact_id)
    if contact:
        ids = list(contact.get("last_processed_event_ids") or [])
        if event_id not in ids:
            ids.append(event_id)
        contact["last_processed_event_ids"] = ids[-200:]
        contact["last_processed_event_id"] = event_id
        if room_id:
            contact["last_polled_event_id"] = event_id
    if room_id:
        room_ids = list(state.setdefault("processed_events", {}).get(room_id) or [])
        if event_id not in room_ids:
            room_ids.append(event_id)
        state["processed_events"][room_id] = room_ids[-500:]
    _save_state(state)


def _remember_seen_event(room_id: str, event_id: str) -> None:
    if not event_id:
        return
    state = _load_state()
    _advance_event_cursor(state, event_id)
    if not room_id:
        _save_state(state)
        return
    contact_id = state.get("rooms", {}).get(room_id)
    contact = state.get("contacts", {}).get(contact_id) if contact_id else None
    if contact:
        contact["last_polled_event_id"] = event_id
    room_ids = list(state.setdefault("processed_events", {}).get(room_id) or [])
    if event_id not in room_ids:
        room_ids.append(event_id)
    state["processed_events"][room_id] = room_ids[-500:]
    _save_state(state)


def _already_processed(contact: dict[str, Any] | None, room_id: str, event_id: str) -> bool:
    if not event_id:
        return False
    if contact and event_id in set(contact.get("last_processed_event_ids") or []):
        return True
    state = _load_state()
    return event_id in set(state.get("processed_events", {}).get(room_id) or [])


def _advance_event_cursor(state: dict[str, Any], event_id: str) -> None:
    if not event_id:
        return
    current = str(state.get("last_event_cursor") or "")
    if not current or event_id > current:
        state["last_event_cursor"] = event_id
        state["last_event_cursor_updated_at"] = _utc_iso()


def _safe_filename(name: str) -> str:
    name = Path(name).name or "media"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def _download_url(url: str, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=120, stream=True)
    response.raise_for_status()
    with path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
    return str(path.resolve())


def download_media(media_id: str, event_id: str = "", client: InterfaceClient | None = None, filename: str = "") -> str:
    if not media_id:
        return ""
    client = client or InterfaceClient()
    info = client.media_show(media_id)
    if not isinstance(info, dict):
        return ""
    url = _first_text(info.get("download_url"), info.get("downloadUrl"), info.get("url"))
    if not url:
        return ""
    if url.startswith("/"):
        try:
            from core.glass import load_glass_config

            config, _ = load_glass_config(PROJECT_ROOT)
            server_url = str(config.get("server_url") or "").rstrip("/")
            if server_url:
                url = server_url + url
        except Exception:
            return ""
    chosen_name = _safe_filename(filename or info.get("filename") or info.get("name") or media_id)
    prefix = _safe_filename(event_id or str(int(_now() * 1000)))
    return _download_url(url, MEDIA_DIR / f"{prefix}_{chosen_name}")


def _transcript_for_event(event: dict[str, Any], local_path: str, media_id: str, client: InterfaceClient) -> str:
    content = _event_content(event)
    transcript = _first_text(event.get("transcript"), content.get("transcript"))
    if transcript:
        return transcript.strip()
    value = local_path or media_id
    if not value:
        return ""
    try:
        payload = client.stt(value)
    except Exception:
        return ""
    if isinstance(payload, dict):
        return _first_text(payload.get("text"), payload.get("transcript"), payload.get("body")).strip()
    return str(payload or "").strip()


def _format_event_context(
    contact_id: str,
    contact: dict[str, Any],
    event: dict[str, Any],
    *,
    local_paths: list[str],
    transcript: str,
) -> str:
    event_type = _event_type(event)
    event_id = _event_id(event)
    room_id = _event_room_id(event) or contact.get("room_id", "")
    body = _event_body(event)
    display_time = _event_display_time(event)
    identity_label = "silicon_id" if contact.get("contact_type") == "silicon" else "carbon_id"
    display_name = contact.get("display_name") or contact.get("name") or contact_id

    lines = [
        f"Interface event from {display_name} ({identity_label}: {contact_id})",
        f"contact_type: {contact.get('contact_type', 'carbon')}",
        f"room_id: {room_id}",
        f"event_id: {event_id}",
        f"event_type: {event_type}",
    ]
    if display_time:
        lines.append(f"display_time: {display_time}")
    reply_to = _event_reply_to(event)
    if reply_to:
        lines.append(f"reply_to: {reply_to}")
    take_back_request_id = _event_take_back_request_id(event)
    if take_back_request_id:
        lines.append(f"take_back_request_id: {take_back_request_id}")
    if body:
        lines.extend(["message:", body])
    if transcript:
        lines.extend(["transcript:", transcript])
    if local_paths:
        lines.append("downloaded_files:")
        lines.extend(f"- {path}" for path in local_paths)
    return "\n".join(lines)


def process_incoming_event(event: dict[str, Any], client: InterfaceClient | None = None) -> tuple[str, str] | None:
    client = client or InterfaceClient()
    state = _load_state()
    event_id = _event_id(event)
    room_id = _event_room_id(event)
    if _event_is_self(event, state):
        _remember_seen_event(room_id, event_id)
        return None

    event_type = _event_type(event)
    if event_type in IGNORED_EVENT_TYPES or event_type not in USER_VISIBLE_EVENT_TYPES:
        _remember_seen_event(room_id, event_id)
        return None

    contact_id, contact, _ = _contact_for_room(room_id, client=client)
    if not contact_id or not contact:
        _remember_seen_event(room_id, event_id)
        return None
    if _already_processed(contact, room_id, event_id):
        _remember_seen_event(room_id, event_id)
        return None

    local_paths: list[str] = []
    media_id = _event_media_id(event)
    local_path = ""
    if media_id:
        try:
            local_path = download_media(media_id, event_id=event_id, client=client, filename=_event_filename(event, media_id))
            if local_path:
                local_paths.append(local_path)
        except Exception as exc:
            local_paths.append(f"download failed for media_id {media_id}: {exc}")

    transcript = ""
    if event_type in {"m.voice", "m.tts"}:
        transcript = _transcript_for_event(event, local_path, media_id, client)

    body = _event_body(event).strip()
    # Log every inbound message; for attachments, resolve and record the S3 link.
    try:
        from core.activity_log import incoming as _log_incoming, url_from
        _att_url = ""
        if media_id:
            try:
                _att_url = url_from(client.media_show(media_id))
            except Exception:
                _att_url = ""
        _log_incoming(contact_id, event_type, body=body, media_id=media_id,
                      attachment_url=_att_url, event_id=event_id)
    except Exception:
        pass
    if event_type == "m.text" and body == "/new":
        context = "[COMMAND: NEW_SESSION]"
    elif event_type == "m.text" and body == "/start":
        context = "[COMMAND: START]"
    else:
        context = _format_event_context(contact_id, contact, event, local_paths=local_paths, transcript=transcript)
    _remember_processed(contact_id, event_id, room_id)
    if room_id and event_id:
        try:
            client.read(room_id, event_id)
        except Exception:
            pass
    return contact_id, context


def _listener_loop(stop_event: threading.Event) -> None:
    global _last_listener_error, _listener_proc
    backoff = 1
    while not stop_event.is_set():
        try:
            client = InterfaceClient()
            try:
                discover_rooms(client)
            except Exception:
                pass
            for event in _sync_events_from_cursor(client, reason="listener"):
                _event_queue.put(event)
            proc = client.listen_all_process()
            _listener_proc = proc
            backoff = 1
            assert proc.stdout is not None
            for line in proc.stdout:
                if stop_event.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    if payload.get("type") == "central_carbon_set":
                        # Glass just resolved who this silicon answers to —
                        # refresh the cached profile + contact flags now.
                        try:
                            _sync_profile_from_glass(client.whoami())
                        except Exception:
                            pass
                        continue
                    if payload.get("type") == "room.added":
                        try:
                            discover_rooms(client, force=True)
                        except Exception:
                            pass
                        continue
                    if isinstance(payload.get("event"), dict):
                        event = dict(payload["event"])
                        if payload.get("room_id") and not event.get("room_id") and not event.get("roomId"):
                            event["room_id"] = payload["room_id"]
                        _event_queue.put(event)
                    else:
                        _event_queue.put(payload)
            if proc.poll() is None:
                proc.wait(timeout=5)
        except InterfaceError as exc:
            if _now() - _last_listener_error > 60:
                print(f"[Interface] listener unavailable: {exc}", flush=True)
                _last_listener_error = _now()
            return
        except Exception as exc:
            if _now() - _last_listener_error > 30:
                print(f"[Interface] listener restarted after error: {exc}", flush=True)
                _last_listener_error = _now()
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            _listener_proc = None


def start_listener() -> None:
    global _listener_thread, _listener_stop
    with _listener_lock:
        if _listener_thread and _listener_thread.is_alive():
            return
        _listener_stop = threading.Event()
        _listener_thread = threading.Thread(target=_listener_loop, args=(_listener_stop,), name="interface-listener", daemon=True)
        _listener_thread.start()


def restart_listener() -> None:
    global _listener_thread, _listener_stop, _listener_proc
    with _listener_lock:
        if _listener_stop:
            _listener_stop.set()
        if _listener_proc and _listener_proc.poll() is None:
            try:
                _listener_proc.terminate()
            except Exception:
                pass
        if _listener_thread and _listener_thread.is_alive():
            _listener_thread.join(timeout=2)
        _listener_stop = threading.Event()
        _listener_thread = threading.Thread(target=_listener_loop, args=(_listener_stop,), name="interface-listener", daemon=True)
        _listener_thread.start()


def _drain_listener_events(max_events: int = 200) -> list[dict[str, Any]]:
    events = []
    for _ in range(max_events):
        try:
            events.append(_event_queue.get_nowait())
        except queue.Empty:
            break
    return events


def _event_from_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(frame, dict) or frame.get("type") != "event":
        return None
    event = frame.get("event")
    if not isinstance(event, dict):
        return None
    payload = dict(event)
    if frame.get("room_id") and not payload.get("room_id") and not payload.get("roomId"):
        payload["room_id"] = frame["room_id"]
    return payload


def _sync_events_from_cursor(client: InterfaceClient, *, reason: str = "") -> list[dict[str, Any]]:
    if not _event_sync_lock.acquire(blocking=False):
        return []
    try:
        state = _load_state()
        after = str(state.get("last_event_cursor") or "")
        events: list[dict[str, Any]] = []
        for _ in range(EVENT_SYNC_MAX_PAGES):
            try:
                payload = client.events_sync(after=after, limit=EVENT_SYNC_LIMIT)
            except Exception as exc:
                if reason:
                    print(f"[Interface] event sync failed ({reason}): {exc}", flush=True)
                return events
            frames = _as_list(payload, ("frames", "data", "results"))
            for frame in frames:
                event = _event_from_frame(frame)
                if event is not None:
                    events.append(event)
            next_after = str(payload.get("next") or "")
            if not payload.get("has_more") or not frames or not next_after or next_after == after:
                break
            after = next_after
        if events:
            label = f" ({reason})" if reason else ""
            print(f"[Interface] synced {len(events)} missed event(s){label}", flush=True)
        return events
    finally:
        _event_sync_lock.release()


def _schedule_next_safety_sync(state: dict[str, Any], now: float | None = None) -> None:
    jitter = random.uniform(0, SAFETY_EVENT_SYNC_JITTER_SECONDS)
    state["next_safety_event_sync"] = (now or _now()) + SAFETY_EVENT_SYNC_SECONDS + jitter


def _maybe_safety_sync(client: InterfaceClient) -> list[dict[str, Any]]:
    now = _now()
    state = _load_state()
    next_at = float(state.get("next_safety_event_sync") or 0)
    if next_at <= 0:
        _schedule_next_safety_sync(state, now)
        _save_state(state)
        return []
    if now < next_at:
        return []

    events = _sync_events_from_cursor(client, reason="safety")
    state = _load_state()
    state["last_event_sync"] = now
    _schedule_next_safety_sync(state, now)
    _save_state(state)
    return events


def _poll_room_events(client: InterfaceClient, state: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for contact in state.get("contacts", {}).values():
        room_id = contact.get("room_id")
        if not room_id:
            continue
        since = contact.get("last_polled_event_id") or contact.get("last_processed_event_id") or ""
        try:
            payload = client.events_list(room_id, since=since)
        except Exception:
            continue
        for item in _as_list(payload, ("events", "data", "results")):
            if not isinstance(item, dict):
                continue
            event = dict(item)
            if not event.get("room_id") and not event.get("roomId"):
                event["room_id"] = room_id
            events.append(event)
    return events


def get_unread_events() -> dict[str, str]:
    """Return manager contexts keyed by fixed contact id."""
    global _boot_event_sync_done
    client = InterfaceClient()
    try:
        discover_rooms(client)
    except InterfaceError as exc:
        print(f"[Interface] {exc}", flush=True)
        return {}
    except Exception as exc:
        print(f"[Interface] room discovery failed: {exc}", flush=True)

    raw_events: list[dict[str, Any]] = []
    if not _boot_event_sync_done:
        raw_events.extend(_sync_events_from_cursor(client, reason="boot"))
        _boot_event_sync_done = True

    start_listener()
    raw_events.extend(_drain_listener_events())
    raw_events.extend(_maybe_safety_sync(client))

    contexts: dict[str, list[str]] = {}
    for event in raw_events:
        try:
            processed = process_incoming_event(event, client=client)
        except Exception as exc:
            print(f"[Interface] event processing failed: {exc}", flush=True)
            continue
        if not processed:
            continue
        contact_id, context = processed
        contexts.setdefault(contact_id, []).append(context)

    return {contact_id: "\n---\n".join(parts) for contact_id, parts in contexts.items() if parts}


def _parse_reply_segments(message: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    last_end = 0
    for match in RICH_MEDIA_RE.finditer(message or ""):
        start, end = match.span()
        text_before = (message[last_end:start] or "").strip()
        if text_before:
            segments.append(("text", text_before))
        segments.append((match.group(1), match.group(2)))
        last_end = end
    text_after = (message[last_end:] or "").strip()
    if text_after:
        segments.append(("text", text_after))
    if not segments:
        segments.append(("text", message or ""))
    return segments


def _contact_room_or_error(contact_id: str) -> tuple[dict[str, Any] | None, str]:
    contact = get_contact(contact_id)
    if not contact:
        return None, f"Error: contact '{contact_id}' not found"
    if not contact.get("room_id"):
        return None, f"Error: contact '{contact_id}' has no Interface DM"
    return contact, ""


def reply_contact(message: str, contact_id: str) -> str:
    contact, err = _contact_room_or_error(contact_id)
    if err:
        return err
    assert contact is not None
    client = InterfaceClient()
    room_id = contact["room_id"]
    errors: list[str] = []
    for seg_type, seg_value in _parse_reply_segments(message):
        try:
            if seg_type == "text":
                if seg_value:
                    client.send(room_id, seg_value)
            elif seg_type == "file":
                path = os.path.abspath(os.path.expanduser(seg_value.strip()))
                if not os.path.exists(path):
                    errors.append(f"File not found: {path}")
                    continue
                sent = client.send_file(room_id, path)
                try:
                    from core.activity_log import attachment, url_from
                    attachment("sent", contact_id, url=url_from(sent), path=path,
                               filename=os.path.basename(path))
                except Exception:
                    pass
            elif seg_type == "voice":
                client.tts(room_id, seg_value)
        except Exception as exc:
            errors.append(f"{seg_type} segment failed: {exc}")
    status = "Sent with errors: " + "; ".join(errors) if errors else "Message sent"
    try:
        from core.activity_log import reply as _log_reply
        _log_reply(contact_id, message, status)
    except Exception:
        pass
    return status


def send_progress(contact_id: str, group: str, state: str, message: str = "") -> None:
    contact = get_contact(contact_id)
    if not contact or not contact.get("room_id"):
        return
    try:
        InterfaceClient().progress(contact["room_id"], group, state, message)
    except Exception:
        pass


def parse_remote_browser_url(stdout: str) -> str:
    match = URL_RE.search(stdout or "")
    return match.group(0).rstrip(".,)") if match else ""


# Maps an active share session ("remote-<contact>") to the interface event_id
# of its card, so `close` can tell the interface to grey that card out.
REMOTE_BROWSER_STATE_FILE = STATE_DIR / "remote_browser.json"


def _extract_event_id(posted: Any) -> str:
    if isinstance(posted, dict):
        ev = posted.get("event") if isinstance(posted.get("event"), dict) else posted
        eid = ev.get("event_id") or ev.get("id")
        if isinstance(eid, str):
            return eid
    return ""


def _extract_remote_browser_url(posted: Any, fallback: str = "") -> str:
    if isinstance(posted, dict):
        ev = posted.get("event") if isinstance(posted.get("event"), dict) else posted
        content = ev.get("content") if isinstance(ev.get("content"), dict) else {}
        url = content.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return fallback


def _load_remote_browser_state() -> dict:
    try:
        return json.loads(REMOTE_BROWSER_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_remote_browser_event(session_name: str, event_id: str) -> None:
    try:
        state = _load_remote_browser_state()
        state[session_name] = event_id
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        REMOTE_BROWSER_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _pop_remote_browser_event(session_name: str) -> str:
    state = _load_remote_browser_state()
    event_id = state.pop(session_name, "")
    if event_id:
        try:
            REMOTE_BROWSER_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        except Exception:
            pass
    return event_id if isinstance(event_id, str) else ""


def remote_browser_share(contact_id: str, expiry: int = 60, new: bool = True) -> str:
    contact, err = _contact_room_or_error(contact_id)
    if err:
        return err
    assert contact is not None

    from worker.handler import SILICON_BROWSER_PROFILE

    minutes = int(expiry or 60)
    session_name = f"remote-{contact_id}"
    cmd = [
        "silicon-browser",
        "--session",
        session_name,
        "--profile",
        SILICON_BROWSER_PROFILE,
        "share",
    ]
    if new:
        cmd.append("--new")
    cmd.extend(["--expiry", str(minutes)])
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        return f"Error: silicon-browser share failed: {output.strip()}"
    url = parse_remote_browser_url(output)
    if not url:
        return f"Error: silicon-browser did not return a share URL: {output.strip()}"

    posted = InterfaceClient().remote_browser(contact["room_id"], url, minutes)
    event_id = _extract_event_id(posted)
    if event_id:
        _save_remote_browser_event(session_name, event_id)
    branded_url = _extract_remote_browser_url(posted, fallback=url)
    return f"Done. Remote browser shared. session={session_name}, expiry_minutes={minutes}, url={branded_url}"


def remote_browser_close(contact_id: str) -> str:
    from worker.handler import SILICON_BROWSER_PROFILE

    session_name = f"remote-{contact_id}"
    cmd = [
        "silicon-browser",
        "--session",
        session_name,
        "--profile",
        SILICON_BROWSER_PROFILE,
        "close",
    ]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"Error: silicon-browser close failed: {output}"

    # Tell the interface the card is closed so it greys out immediately,
    # rather than counting down to its original expiry. Best-effort.
    event_id = _pop_remote_browser_event(session_name)
    if event_id:
        try:
            from core.glass import silicon_api_post

            silicon_api_post(f"/api/v1/events/{event_id}/remote_browser_close")
        except Exception as exc:  # noqa: BLE001 — close must not fail on the card update
            return (
                f"Done. Remote browser closed. session={session_name}. Profile state saved. "
                f"(card update skipped: {exc})"
            )
    return f"Done. Remote browser closed. session={session_name}. Profile state saved."


def complete_take_back(request_id: str, replacement: str) -> str:
    if not request_id:
        return "Error: request_id is required"
    payload = InterfaceClient().take_back_complete(request_id, replacement or "")
    return "Done. Take-back completed." + (f" {json.dumps(payload)}" if payload else "")


def take_back_event(event_id: str, reason: str = "", force: bool = False) -> str:
    if not event_id:
        return "Error: event_id is required"
    payload = InterfaceClient().take_back_event(event_id, reason=reason, force=force)
    return "Done. Event take-back requested." + (f" {json.dumps(payload)}" if payload else "")
