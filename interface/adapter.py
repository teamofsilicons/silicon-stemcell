"""Silicon Interface transport, local contacts, and event ingestion.

Interface and Glass own the wire. Glass owns canonical contact trust; Stemcell
caches and enforces the last confirmed policy locally. Stemcell owns manager
state, processed watermarks, and downloaded media paths.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

import requests

from helpers.process import submit_best_effort
from helpers.watch import PathChangeWaiter, PathSetChangeWaiter
from helpers.paths import DATA_ROOT
from helpers.state import file_lock, read_json, update_json, write_json

PROJECT_ROOT = DATA_ROOT
STATE_DIR = PROJECT_ROOT / "core" / "interface_state"
CONTACTS_FILE = STATE_DIR / "contacts.json"
CONTACTS_BACKUP_FILE = STATE_DIR / "contacts_backup.json"
MEDIA_DIR = STATE_DIR / "media"
INBOX_CONSUMER_FILE = STATE_DIR / "interface_inbox_consumer.json"
DEFAULT_INBOX_FILE = PROJECT_ROOT / ".silicon-interface" / "inbox.jsonl"
LEGACY_TELEGRAM_CONTACTS_FILE = PROJECT_ROOT / "core" / "telegram" / "contacts.json"

VALID_TRUST_LEVELS = ["very_low", "low", "ok", "high", "very_high", "ultimate"]
USER_VISIBLE_EVENT_TYPES = {"m.text", "m.image", "m.file", "m.album", "m.voice", "m.tts"}
IGNORED_EVENT_TYPES = {"m.progress", "m.reaction", "m.session_marker", "m.system"}
RICH_MEDIA_RE = re.compile(r"\[(file|voice)=((?:[^\[\]]|\[[^\]]*\])*)\]", re.DOTALL)
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
REMOTE_BROWSER_START_URL = os.environ.get("SILICON_REMOTE_BROWSER_START_URL", "https://www.google.com")

_listener_thread: threading.Thread | None = None
_listener_lock = threading.Lock()
_listener_stop: threading.Event | None = None
_runtime_file_thread: threading.Thread | None = None
_runtime_file_lock = threading.Lock()
_runtime_file_stop: threading.Event | None = None
_runtime_file_paths: tuple[str, ...] = ()
_runtime_file_native = False
_event_queue: "queue.Queue[InboxRecord | dict[str, Any]]" = queue.Queue()
_inbox_retry_records: "deque[InboxRecord]" = deque()
_inbox_retry_lock = threading.Lock()
_activity_condition = threading.Condition()
_activity_pending = 0
_last_listener_error = 0.0
_inbox_scan_lock = threading.Lock()
_inbox_scan_state: dict[str, Any] = {}
_state_lock = threading.RLock()
_maintenance_notice_lock = threading.Lock()
_maintenance_notice_running = False

INBOX_POLL_SECONDS = 0.1
RUNTIME_FILE_POLL_SECONDS = 0.5
DAEMON_HEALTH_SECONDS = 15
DAEMON_DEEP_HEALTH_SECONDS = 5 * 60
DAEMON_DEEP_HEALTH_JITTER_SECONDS = 60
ROOM_SYNC_FALLBACK_SECONDS = 15 * 60
INBOX_READ_CHUNK_BYTES = 4 * 1024 * 1024
RPC_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class InboxRecord:
    """One complete durable CLI inbox line and its commit boundary."""

    frame: dict[str, Any]
    path: str = ""
    file_id: str = ""
    end_offset: int = 0


class InterfaceError(RuntimeError):
    pass


class _RPCUnavailable(RuntimeError):
    """The daemon socket was unavailable before a request could be sent."""


class _RPCUnsupported(RuntimeError):
    """The daemon rejected a command before dispatch, so CLI fallback is safe."""


class WorkCallMutationError(InterfaceError):
    """Body-free structured failure for retryable call mutations."""

    def __init__(
        self,
        *,
        status_code: int = 0,
        code: str = "",
        current_revision: int | None = None,
        retryable: bool = False,
    ):
        self.status_code = int(status_code or 0)
        self.code = str(code or "")[:80]
        self.current_revision = current_revision
        self.retryable = bool(retryable)
        suffix = f" HTTP {self.status_code}" if self.status_code else ""
        super().__init__(f"Work call mutation failed{suffix}.")


class CallBookkeepingError(InterfaceError):
    """A body-free signal that a durable call intent was not committed."""


class DurableHandoffError(InterfaceError):
    """A body-free signal that manager-root ownership was not confirmed."""


def _state_serialized(func):
    @wraps(func)
    def locked(*args, **kwargs):
        with _state_lock, file_lock(CONTACTS_FILE):
            return func(*args, **kwargs)

    return locked


def _now() -> float:
    return time.time()


def _utc_iso(ts: float | None = None) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts or _now(), tz=timezone.utc).isoformat()




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
    if not LEGACY_TELEGRAM_CONTACTS_FILE.exists():
        return None
    legacy = read_json(LEGACY_TELEGRAM_CONTACTS_FILE, {})
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


@_state_serialized
def _load_state() -> dict[str, Any]:
    if not CONTACTS_FILE.exists():
        migrated = _migrate_legacy_contacts()
        if migrated:
            _save_state(migrated)
    state = read_json(CONTACTS_FILE, _default_contacts_state())
    state.setdefault("version", 1)
    state.setdefault("contacts", {})
    state.setdefault("rooms", {})
    state.setdefault("processed_events", {})
    state.setdefault("work_event_refs", {})
    state.setdefault("own_ids", [])
    state.setdefault("last_room_sync", 0)
    state.setdefault("last_seen_event_id", "")
    return state


@_state_serialized
def _save_state(state: dict[str, Any]) -> None:
    write_json(CONTACTS_FILE, state)


def get_contacts() -> dict[str, Any]:
    return _load_state()


def get_contact(contact_id: str) -> dict[str, Any] | None:
    return _load_state().get("contacts", {}).get(contact_id)


@_state_serialized
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
        kind = _normalize_contact_type(contact.get("contact_type", "carbon"))
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


@_state_serialized
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
        backup_matches = False
        try:
            backup_matches = (
                CONTACTS_BACKUP_FILE.exists()
                and CONTACTS_FILE.read_bytes()
                == CONTACTS_BACKUP_FILE.read_bytes()
            )
        except OSError:
            backup_matches = False
        if not backup_matches:
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

    def rpc_socket_path(self) -> Path:
        configured = str(
            os.environ.get("SILICON_INTERFACE_RPC_SOCKET") or ""
        ).strip()
        if configured:
            return Path(configured).expanduser()
        root = Path(
            os.environ.get("SILICON_INTERFACE_ROOT") or self.cwd
        ).expanduser().resolve()
        state_dir = root / ".silicon-interface"
        discovery = state_dir / "daemon-rpc.json"
        try:
            value = json.loads(discovery.read_text(encoding="utf-8"))
            socket_value = str(value.get("socket") or "")
            if value.get("version") == 1 and socket_value:
                candidate = Path(socket_value).expanduser()
                if candidate.is_absolute():
                    return candidate
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return state_dir / "daemon.sock"

    def _run_rpc(self, args: list[str], *, timeout: int, check: bool) -> Any:
        request_id = secrets.token_hex(16)
        request = json.dumps(
            {
                "version": 1,
                "id": request_id,
                "command": str(args[0]),
                "args": [str(value) for value in args[1:]],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(max(0.1, float(timeout)))
        sent = False
        try:
            try:
                connection.connect(str(self.rpc_socket_path()))
            except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError) as exc:
                raise _RPCUnavailable(str(exc)) from exc
            # Once sendall begins, a retry through a subprocess could duplicate
            # a mutation whose response was lost. Ambiguous failures therefore
            # fail closed instead of silently changing transports.
            sent = True
            connection.sendall(request)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = connection.recv(min(64 * 1024, RPC_MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > RPC_MAX_RESPONSE_BYTES:
                    raise InterfaceError("Interface daemon RPC response exceeded its safe limit")
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks).split(b"\n", 1)[0]
            if not raw:
                raise InterfaceError("Interface daemon RPC closed without a response")
            try:
                response = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InterfaceError("Interface daemon RPC returned invalid JSON") from exc
            if (
                not isinstance(response, dict)
                or response.get("version") != 1
                or response.get("id") != request_id
            ):
                raise InterfaceError("Interface daemon RPC returned a mismatched response")
            if response.get("ok") is True:
                return response.get("result")
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            code = str(error.get("code") or "RPC_ERROR")
            if code == "UNSUPPORTED_COMMAND":
                raise _RPCUnsupported(str(error.get("message") or code))
            if not check:
                return {}
            status = int(error.get("status") or 0)
            detail = str(error.get("message") or code)
            if status:
                detail = f"api {status}: {detail}"
            body = error.get("body")
            if body is not None:
                detail += "\n" + json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            raise InterfaceError(detail)
        except _RPCUnsupported:
            raise
        except _RPCUnavailable:
            raise
        except InterfaceError:
            raise
        except (OSError, TimeoutError) as exc:
            if not sent:
                raise _RPCUnavailable(str(exc)) from exc
            raise InterfaceError(f"Interface daemon RPC outcome is unknown: {exc}") from exc
        finally:
            connection.close()

    def run(
        self,
        args: list[str],
        *,
        json_mode: bool = True,
        input_text: str | None = None,
        timeout: int = 60,
        check: bool = True,
    ) -> Any:
        normalized_args = [str(arg) for arg in args if arg is not None]
        if json_mode and input_text is None and normalized_args:
            try:
                return self._run_rpc(
                    normalized_args,
                    timeout=timeout,
                    check=check,
                )
            except (_RPCUnavailable, _RPCUnsupported):
                # Older daemons and intentionally unsupported interactive
                # commands retain the proven subprocess compatibility path.
                pass
        cmd = self.base_cmd(json_mode=json_mode) + normalized_args
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

    def daemon_status(self) -> Any:
        """Return the CLI v2 durable-listener status and inbox location."""
        payload = self.run(["daemon", "status"], timeout=30, check=False)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("running"), bool)
            or not str(payload.get("inbox") or "").strip()
            or not isinstance(payload.get("cursors"), dict)
        ):
            raise InterfaceError(
                "Silicon Interface CLI v2 is required: `si --json daemon status` "
                "did not return the durable listener contract."
            )
        return payload

    def daemon_local_status(self) -> dict[str, Any]:
        """Check the CLI-owned daemon without cold-starting the Node CLI."""
        root = Path(
            os.environ.get("SILICON_INTERFACE_ROOT") or self.cwd
        ).expanduser().resolve()
        state_dir = root / ".silicon-interface"
        pid_file = state_dir / "daemon.pid"
        pid: int | None = None
        try:
            parsed = int(pid_file.read_text(encoding="utf-8").strip())
            if parsed > 0:
                pid = parsed
        except (OSError, TypeError, ValueError):
            pid = None
        running = False
        if pid is not None:
            try:
                os.kill(pid, 0)
                running = True
            except (OSError, ProcessLookupError, PermissionError):
                running = False
        inbox_value = str(
            os.environ.get("SILICON_INTERFACE_INBOX") or ""
        ).strip()
        inbox = (
            Path(inbox_value).expanduser()
            if inbox_value
            else state_dir / "inbox.jsonl"
        )
        return {
            "running": running,
            "pid": pid,
            "inbox": str(inbox),
        }

    def daemon_start(self) -> str:
        """Start the single CLI-owned listener; this command prints prose."""
        return str(
            self.run(
                ["daemon", "start"],
                json_mode=False,
                timeout=60,
            )
            or ""
        )

    def inbox_path(self) -> Path:
        status = self.daemon_status()
        value = str(status.get("inbox") or "").strip()
        return Path(value).expanduser() if value else DEFAULT_INBOX_FILE

    def send(
        self,
        room_id: str,
        message: str,
        progress_group_id: str = "",
        work_continues: bool = False,
        client_id: str = "",
    ) -> Any:
        args = ["send", room_id, message]
        if client_id:
            args.extend(["--client-id", str(client_id)])
        if progress_group_id:
            args.extend(["--group", progress_group_id])
        if work_continues:
            args.append("--work-continues")
        return self.run(args, timeout=60)

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

    def progress(
        self,
        room_id: str,
        group: str,
        state: str,
        message: str,
        frame_id: str,
        task_id: str = "",
        revision: int | None = None,
        occurred_at: str = "",
        progress_pct: float | None = None,
        summary: str = "",
    ) -> Any:
        args = ["progress", room_id, state, "--group", group]
        if message:
            args.extend(["--note", message])
        args.extend(["--frame", frame_id])
        if task_id:
            args.extend(["--task", task_id])
        if revision is not None:
            args.extend(["--revision", str(revision)])
        if occurred_at:
            args.extend(["--at", occurred_at])
        if progress_pct is not None:
            args.extend(["--pct", str(progress_pct)])
        if summary:
            args.extend(["--summary", summary])
        return self.run(args, timeout=30)

    @staticmethod
    def _compact_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _work_mutation(self, args: list[str], payload: dict[str, Any]) -> Any:
        return self.run(
            [*args, "--data", self._compact_json(payload)],
            timeout=60,
        )

    def _work_call_patch_mutation(
        self,
        args: list[str],
        payload: dict[str, Any],
    ) -> Any:
        try:
            return self._work_mutation(args, payload)
        except InterfaceError as exc:
            # CLI 2.0.2 prints Glass's structured failure after an "api NNN"
            # prefix. Keep only retry metadata; never retain the command or
            # transcript-bearing payload in retry state.
            detail = str(exc)
            status_match = re.search(r"\bapi\s+([1-5][0-9]{2})\b", detail)
            revision_match = re.search(
                r'"current"\s*:\s*\{[^{}]*"revision"\s*:\s*(\d+)',
                detail,
            )
            code_match = re.search(r'"code"\s*:\s*"([^"]+)"', detail)
            status = int(status_match.group(1)) if status_match else 0
            revision = (
                int(revision_match.group(1)) if revision_match else None
            )
            raise WorkCallMutationError(
                status_code=status,
                code=code_match.group(1) if code_match else "",
                current_revision=revision,
                retryable=status
                in {0, 408, 409, 425, 429, 500, 502, 503, 504},
            ) from exc

    def work_task_create(self, payload: dict[str, Any]) -> Any:
        return self._work_mutation(["work", "task", "create"], payload)

    def work_task_show(self, task_id: str) -> Any:
        return self.run(["work", "task", "show", task_id], timeout=60)

    def work_task_patch(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(["work", "task", "patch", task_id], payload)

    def work_todo_add(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(["work", "todo", "add", task_id], payload)

    def work_todo_patch(
        self,
        task_id: str,
        todo_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "todo", "patch", task_id, todo_id],
            payload,
        )

    def work_milestone_create(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(
            ["work", "milestone", "update", task_id],
            payload,
        )

    def work_blocker_create(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(
            ["work", "blocker", "create", task_id],
            payload,
        )

    def work_blocker_resolve(
        self,
        task_id: str,
        blocker_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "blocker", "resolve", task_id, blocker_id],
            payload,
        )

    def work_worker_group_create(
        self,
        task_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "worker-group", "create", task_id],
            payload,
        )

    def work_worker_group_patch(
        self,
        task_id: str,
        group_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "worker-group", "patch", task_id, group_id],
            payload,
        )

    def work_worker_create(
        self,
        task_id: str,
        group_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "worker", "create", task_id, group_id],
            payload,
        )

    def work_worker_patch(
        self,
        task_id: str,
        group_id: str,
        invocation_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            [
                "work",
                "worker",
                "patch",
                task_id,
                group_id,
                invocation_id,
            ],
            payload,
        )

    def work_call_create(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(
            ["work", "call", "create", task_id],
            payload,
        )

    def work_standalone_call_create(self, payload: dict[str, Any]) -> Any:
        return self._work_mutation(
            ["work", "call", "create"],
            payload,
        )

    def work_call_patch(
        self,
        task_id: str,
        call_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_call_patch_mutation(
            ["work", "call", "patch", task_id, call_id],
            payload,
        )

    def work_standalone_call_patch(
        self,
        call_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_call_patch_mutation(
            ["work", "call", "patch", call_id],
            payload,
        )

    def work_task_transition(
        self,
        task_id: str,
        transition: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(["work", transition, task_id], payload)

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


@_state_serialized
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


@_state_serialized
def _cache_own_ids(own_ids: list[str]) -> list[str]:
    """Merge the latest identity lookup into current state without stale writes."""
    normalized = sorted({str(value) for value in own_ids if value})
    if normalized:
        state = _load_state()
        state["own_ids"] = normalized
        _save_state(state)
    return normalized


@_state_serialized
def _finish_room_sync() -> dict[str, Any]:
    """Timestamp room discovery against the newest state written by ingestion."""
    state = _load_state()
    state["last_room_sync"] = _now()
    _save_state(state)
    return state


def discover_rooms(client: InterfaceClient | None = None, *, force: bool = False) -> dict[str, Any]:
    client = client or InterfaceClient()
    state = _load_state()
    if (
        not force
        and _now() - float(state.get("last_room_sync") or 0)
        < ROOM_SYNC_FALLBACK_SECONDS
    ):
        return state

    me_payload = None
    own_ids = list(state.get("own_ids") or [])
    if not own_ids:
        try:
            me_payload = client.whoami()
            own_ids = _extract_own_ids(me_payload)
            if own_ids:
                own_ids = _cache_own_ids(own_ids)
        except Exception:
            own_ids = _load_state().get("own_ids", [])

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

    return _finish_room_sync()


@_state_serialized
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


def _event_media_references(event: dict[str, Any]) -> list[tuple[str, str]]:
    """Return all attachment IDs and filenames in display order."""
    content = _event_content(event)
    if _event_type(event) != "m.album":
        media_id = _event_media_id(event)
        return [(media_id, _event_filename(event, media_id))] if media_id else []

    references: list[tuple[str, str]] = []
    seen: set[str] = set()
    collections = (
        content.get("items"),
        event.get("media_items"),
        content.get("media_items"),
        event.get("items"),
    )
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            media_id = _first_text(item.get("media_id"), item.get("mediaId"), item.get("id"))
            if not media_id or media_id in seen:
                continue
            filename = _first_text(item.get("filename"), item.get("file_name"), item.get("name"))
            references.append((media_id, Path(filename).name if filename else media_id))
            seen.add(media_id)

    if references:
        return references
    media_id = _event_media_id(event)
    return [(media_id, _event_filename(event, media_id))] if media_id else []


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


@_state_serialized
def _remember_work_event_reference(event: dict[str, Any]) -> None:
    """Cache an outer chat Event id -> durable work resource correlation."""
    if _event_type(event) != "m.work_event":
        return
    event_id = _event_id(event)
    room_id = _event_room_id(event)
    content = _event_content(event)
    task_id = _first_text(content.get("task_id"), event.get("task_id"))
    kind = _first_text(content.get("kind"), event.get("kind"))
    if not event_id or not room_id or not kind:
        return
    reference = {
        "kind": kind,
        "work_event_id": _first_text(
            content.get("work_event_id"),
            event.get("work_event_id"),
        ),
    }
    if task_id:
        reference["task_id"] = task_id
    for key in ("blocker_id", "group_id", "call_id"):
        value = _first_text(content.get(key), event.get(key))
        if value:
            reference[key] = value
    state = _load_state()
    room_refs = state.setdefault("work_event_refs", {}).setdefault(room_id, {})
    room_refs[event_id] = reference
    if len(room_refs) > 500:
        for stale_id in list(room_refs)[: len(room_refs) - 500]:
            room_refs.pop(stale_id, None)
    _save_state(state)


def _work_event_reference(room_id: str, event_id: str) -> dict[str, Any]:
    if not room_id or not event_id:
        return {}
    value = (
        _load_state()
        .get("work_event_refs", {})
        .get(room_id, {})
        .get(event_id)
    )
    return value if isinstance(value, dict) else {}


@_state_serialized
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


@_state_serialized
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
    # Diagnostic breadcrumb only. CLI v2 owns the real signed/vector cursor;
    # event IDs must never be compared or used as transport checkpoints.
    state["last_seen_event_id"] = event_id
    state["last_seen_event_updated_at"] = _utc_iso()


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


def _download_media_with_info(
    media_id: str,
    event_id: str = "",
    client: InterfaceClient | None = None,
    filename: str = "",
) -> tuple[str, dict[str, Any]]:
    if not media_id:
        return "", {}
    client = client or InterfaceClient()
    info = client.media_show(media_id)
    if not isinstance(info, dict):
        return "", {}
    url = _first_text(info.get("download_url"), info.get("downloadUrl"), info.get("url"))
    if not url:
        return "", dict(info)
    if url.startswith("/"):
        try:
            from interface.config import load_glass_config

            config, _ = load_glass_config(PROJECT_ROOT)
            server_url = str(config.get("server_url") or "").rstrip("/")
            if server_url:
                url = server_url + url
        except Exception:
            return "", dict(info)
    chosen_name = _safe_filename(filename or info.get("filename") or info.get("name") or media_id)
    prefix = _safe_filename(event_id or str(int(_now() * 1000)))
    return _download_url(url, MEDIA_DIR / f"{prefix}_{chosen_name}"), dict(info)


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
        work_reference = _work_event_reference(room_id, reply_to)
        if work_reference:
            lines.append(
                "reply_to_work_update: "
                + json.dumps(
                    work_reference,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
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


def _record_incoming_bookkeeping(
    contact_id: str,
    contact: dict[str, Any],
    event_type: str,
    event_id: str,
    body: str,
    media_id: str,
    media_info: dict[str, Any],
) -> None:
    """Persist ancillary transcript/activity data off the ingestion path."""
    try:
        from diagnostics.activity import incoming as _log_incoming, url_from

        attachment_url = url_from(media_info) if media_id else ""
        _log_incoming(
            contact_id,
            event_type,
            body=body,
            media_id=media_id,
            attachment_url=attachment_url,
            event_id=event_id,
        )
    except Exception:
        pass
    try:
        # A reply arriving is what makes earlier messages "read" as far as
        # `iwantto see --unread` is concerned, so inbound is recorded too.
        from diagnostics.iwantto.message_log import record_inbound

        record_inbound(
            contact_id,
            event_id,
            body,
            "file" if media_id else "text",
        )
    except Exception:
        pass


def _record_incoming_call_bookkeeping(
    contact_id: str,
    contact: dict[str, Any],
    body: str,
    event_id: str,
) -> None:
    """Journal Silicon call transcript state before the in-memory outbox."""
    if (
        contact.get("contact_type") != "silicon"
        or not body
        or not event_id
    ):
        return
    try:
        from interface.work_updates import (
            enqueue_inbound_call,
            record_contact_call_message,
        )

        appended = record_contact_call_message(
            contact_id,
            speaker_kind="silicon",
            speaker_id=str(contact.get("silicon_id") or contact_id),
            speaker_name=str(
                contact.get("display_name")
                or contact.get("name")
                or contact_id
            ),
            message=body,
            idempotency_key=f"incoming-call:{contact_id}:{event_id}",
            terminal=True,
        )
        if not appended:
            enqueue_inbound_call(
                contact_id,
                source_kind="silicon",
                source_id=str(contact.get("silicon_id") or contact_id),
                source_name=str(
                    contact.get("display_name")
                    or contact.get("name")
                    or contact_id
                ),
                message=body,
                idempotency_key=f"incoming-call:{contact_id}:{event_id}",
            )
    except Exception as exc:
        # This is intentionally raised before the processed watermark. The
        # durable inbox record remains uncommitted and will replay with the
        # same event-derived idempotency key.
        raise CallBookkeepingError(
            "Incoming call bookkeeping was not durably committed."
        ) from exc


def _send_read_receipt(client: InterfaceClient, room_id: str, event_id: str) -> None:
    try:
        client.read(room_id, event_id)
    except Exception:
        pass


def process_incoming_event(
    event: dict[str, Any],
    client: InterfaceClient | None = None,
    *,
    defer_processed_watermark: bool = False,
) -> tuple[str, str] | None:
    client = client or InterfaceClient()
    state = _load_state()
    event_id = _event_id(event)
    room_id = _event_room_id(event)
    event_type = _event_type(event)
    if event_type == "m.work_event":
        try:
            _remember_work_event_reference(event)
        except Exception:
            pass
    if _event_is_self(event, state):
        if event_type == "m.text" and event_id:
            contact_id, contact, _ = _contact_for_room(room_id, client=client)
            if (
                contact_id
                and isinstance(contact, dict)
                and contact.get("contact_type") == "silicon"
            ):
                _record_sent_call_message(
                    contact_id,
                    _event_body(event).strip(),
                    event_id,
                )
        _remember_seen_event(room_id, event_id)
        return None

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

    trace = None
    ingest_span = None
    try:
        from diagnostics.store import Diagnostics

        active_trace = Diagnostics.get_active_run(contact_id)
        # An event that arrives while this contact's manager is already
        # running belongs to the dispatcher's next turn. Attaching it to the
        # current run would attribute the same event to two diagnostic graphs.
        if active_trace is not None and active_trace.meta.get("_manager_running"):
            trace = None
        elif active_trace is None:
            trace = Diagnostics.start_run(
                trigger="message",
                carbon_id=contact_id,
                room_id=room_id,
                message_ids=[event_id] if event_id else [],
            )
            Diagnostics.register_active(contact_id, trace)
        else:
            trace = active_trace
            trace.add_message(event_id, room_id)
        if trace is not None:
            trace.event("message.ingress", event_id=event_id, room_id=room_id, event_type=event_type)
            ingest_span = trace.span("interface.message_ingest")
            ingest_span.__enter__()
            ingest_span.set_meta(event_id=event_id, room_id=room_id, event_type=event_type)
    except Exception:
        trace = None
        ingest_span = None

    local_paths: list[str] = []
    media_references = _event_media_references(event)
    media_id = media_references[0][0] if media_references else ""
    media_info: dict[str, Any] = {}
    local_path = ""
    for index, (item_media_id, filename) in enumerate(media_references):
        try:
            item_event_id = event_id
            if len(media_references) > 1:
                item_event_id = f"{event_id}_{index + 1}" if event_id else str(index + 1)
            local_path, item_media_info = _download_media_with_info(
                item_media_id,
                event_id=item_event_id,
                client=client,
                filename=filename,
            )
            if index == 0:
                media_info = item_media_info
            if local_path:
                local_paths.append(local_path)
        except Exception as exc:
            local_paths.append(f"download failed for media_id {item_media_id}: {exc}")

    transcript = ""
    if event_type in {"m.voice", "m.tts"}:
        transcript = _transcript_for_event(event, local_path, media_id, client)

    body = _event_body(event).strip()
    if event_type == "m.text" and body == "/new":
        context = "[COMMAND: NEW_SESSION]"
    elif event_type == "m.text" and body == "/start":
        context = "[COMMAND: START]"
    else:
        context = _format_event_context(contact_id, contact, event, local_paths=local_paths, transcript=transcript)
    try:
        _record_incoming_call_bookkeeping(
            contact_id,
            dict(contact),
            body,
            event_id,
        )
    except CallBookkeepingError:
        if ingest_span is not None:
            ingest_span.__exit__(None, None, None)
        raise
    if not defer_processed_watermark:
        _remember_processed(contact_id, event_id, room_id)
    submit_best_effort(
        _record_incoming_bookkeeping,
        contact_id,
        dict(contact),
        event_type,
        event_id,
        body,
        media_id,
        media_info,
        key=f"incoming-bookkeeping:{contact_id}",
    )
    if room_id and event_id:
        submit_best_effort(
            _send_read_receipt,
            client,
            room_id,
            event_id,
            key=f"read-receipt:{room_id}",
            coalesce=True,
        )
    if ingest_span is not None:
        ingest_span.__exit__(None, None, None)
    return contact_id, context


def _inbox_file_id(stat_result: os.stat_result) -> str:
    return f"{getattr(stat_result, 'st_dev', 0)}:{getattr(stat_result, 'st_ino', 0)}"


def _load_inbox_consumer() -> dict[str, Any]:
    state = read_json(INBOX_CONSUMER_FILE, {})
    if not isinstance(state, dict) or state.get("version") != 1:
        return {"version": 1, "path": "", "file_id": "", "offset": 0}
    try:
        offset = max(0, int(state.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    return {
        "version": 1,
        "path": str(state.get("path") or ""),
        "file_id": str(state.get("file_id") or ""),
        "offset": offset,
    }


def _save_inbox_consumer(path: str, file_id: str, offset: int) -> None:
    write_json(
        INBOX_CONSUMER_FILE,
        {
            "version": 1,
            "path": path,
            "file_id": file_id,
            "offset": max(0, int(offset)),
            "updated_at": _utc_iso(),
        },
    )


def _read_new_inbox_records(path: Path, *, max_records: int = 500) -> list[InboxRecord]:
    """Read complete, not-yet-committed CLI inbox lines without acknowledging them.

    The in-memory scan offset prevents duplicate queueing while this process is
    alive. The durable offset advances only after the main loop has interpreted
    a record, so a crash before interpretation replays it on restart.
    """
    resolved = str(path.expanduser().resolve())
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return []
    file_id = _inbox_file_id(stat_result)

    with _inbox_scan_lock:
        cursor = _load_inbox_consumer()
        scan = dict(_inbox_scan_state)
        if (
            scan.get("path") != resolved
            or scan.get("file_id") != file_id
            or int(scan.get("offset") or 0) > stat_result.st_size
        ):
            if (
                cursor.get("path") == resolved
                and cursor.get("file_id") == file_id
                and int(cursor.get("offset") or 0) <= stat_result.st_size
            ):
                offset = int(cursor.get("offset") or 0)
            else:
                # Rotation, truncation, or first use: scan the replacement from
                # its beginning. Processed event IDs make snapshot replay safe.
                offset = 0
            scan = {"path": resolved, "file_id": file_id, "offset": offset}

        records: list[InboxRecord] = []
        bytes_read = 0
        with path.open("rb") as inbox:
            inbox.seek(int(scan["offset"]))
            while len(records) < max_records and bytes_read < INBOX_READ_CHUNK_BYTES:
                start = inbox.tell()
                line = inbox.readline()
                if not line:
                    break
                # The daemon may be between its append and newline. Leave the
                # partial line unscanned until the next pass.
                if not line.endswith(b"\n"):
                    inbox.seek(start)
                    break
                end = inbox.tell()
                bytes_read += end - start
                try:
                    payload = json.loads(line.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("inbox frame is not an object")
                    frame = payload
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    # A complete malformed line cannot become valid later.
                    # Advance past it without echoing potentially private data.
                    frame = {"type": "_invalid_inbox_line"}
                records.append(
                    InboxRecord(
                        frame=frame,
                        path=resolved,
                        file_id=file_id,
                        end_offset=end,
                    )
                )
            scan["offset"] = inbox.tell()
        _inbox_scan_state.clear()
        _inbox_scan_state.update(scan)
        return records


def _commit_inbox_record(record: InboxRecord) -> None:
    if not record.path or not record.file_id or record.end_offset <= 0:
        return
    with file_lock(INBOX_CONSUMER_FILE):
        cursor = _load_inbox_consumer()
        if (
            cursor.get("path") == record.path
            and cursor.get("file_id") == record.file_id
            and int(cursor.get("offset") or 0) >= record.end_offset
        ):
            return
        _save_inbox_consumer(record.path, record.file_id, record.end_offset)


def _queue_inbox_records(records: list[InboxRecord]) -> None:
    if not records:
        return
    for record in records:
        _event_queue.put(record)
    notify_runtime_activity()


def notify_runtime_activity() -> None:
    """Wake the main runtime for Interface or local-manager work."""
    global _activity_pending
    with _activity_condition:
        _activity_pending += 1
        _activity_condition.notify()


def wait_for_runtime_activity(timeout: float) -> bool:
    """Wait until durable input arrives, without losing a concurrent wakeup."""
    global _activity_pending
    deadline = time.monotonic() + max(0.0, float(timeout))
    with _activity_condition:
        while _activity_pending <= 0 and _event_queue.empty():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _activity_condition.wait(remaining)
        if _activity_pending > 0:
            _activity_pending -= 1
        return True


def _listener_loop(stop_event: threading.Event) -> None:
    """Keep the CLI v2 daemon healthy and tail its durable inbox."""
    global _last_listener_error
    backoff = 1.0
    while not stop_event.is_set():
        try:
            client = InterfaceClient()
            status = client.daemon_local_status()
            if not status.get("running"):
                client.daemon_start()
                deadline = _now() + 2.0
                while not stop_event.is_set() and _now() < deadline:
                    status = client.daemon_local_status()
                    if status.get("running"):
                        break
                    stop_event.wait(0.05)
            if not status.get("running"):
                raise InterfaceError("Silicon Interface durable inbox daemon did not start.")

            # One full contract probe confirms that the process behind the PID
            # is actually the expected daemon. Subsequent frequent checks stay
            # process-local; the deep probe uses the daemon RPC and runs only as
            # a jittered safety check.
            status = client.daemon_status()
            if not status.get("running"):
                raise InterfaceError("Silicon Interface daemon failed its contract probe.")
            inbox_value = str(status.get("inbox") or "").strip()
            inbox_path = Path(inbox_value).expanduser() if inbox_value else DEFAULT_INBOX_FILE
            if not inbox_path.is_absolute():
                inbox_path = PROJECT_ROOT / inbox_path

            backoff = 1.0
            next_local_health = _now() + DAEMON_HEALTH_SECONDS
            jitter_digest = hashlib.sha256(
                f"{PROJECT_ROOT}:interface-deep-health".encode("utf-8")
            ).digest()
            deep_jitter = int.from_bytes(jitter_digest[:2], "big") % (
                DAEMON_DEEP_HEALTH_JITTER_SECONDS + 1
            )
            next_deep_health = (
                _now() + DAEMON_DEEP_HEALTH_SECONDS + deep_jitter
            )
            with PathChangeWaiter(
                inbox_path,
                fallback_poll_seconds=INBOX_POLL_SECONDS,
            ) as inbox_changes:
                while not stop_event.is_set():
                    _queue_inbox_records(_read_new_inbox_records(inbox_path))
                    now = _now()
                    if now >= next_local_health:
                        status = client.daemon_local_status()
                        if not status.get("running"):
                            break
                        next_local_health = now + DAEMON_HEALTH_SECONDS
                    if now >= next_deep_health:
                        status = client.daemon_status()
                        if not status.get("running"):
                            break
                        next_deep_health = (
                            now + DAEMON_DEEP_HEALTH_SECONDS + deep_jitter
                        )
                    inbox_changes.wait(
                        max(
                            0.0,
                            min(next_local_health, next_deep_health) - _now(),
                        ),
                        stop_event,
                    )
        except Exception as exc:
            if _now() - _last_listener_error > 30:
                print(f"[Interface] durable inbox unavailable: {exc}", flush=True)
                _last_listener_error = _now()
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 30.0)


def start_listener() -> None:
    global _listener_thread, _listener_stop
    with _listener_lock:
        if _listener_thread and _listener_thread.is_alive():
            return
        _listener_stop = threading.Event()
        _listener_thread = threading.Thread(target=_listener_loop, args=(_listener_stop,), name="interface-listener", daemon=True)
        _listener_thread.start()


def stop_listener() -> None:
    """Stop only Stemcell's inbox tailer; the CLI daemon stays durable."""
    global _listener_thread, _listener_stop
    with _listener_lock:
        stop_event = _listener_stop
        thread = _listener_thread
        if stop_event:
            stop_event.set()
        if thread and thread.is_alive():
            thread.join(timeout=2)
        # Retain a timed-out thread reference so maintenance cannot attest
        # inbox quiescence while that old tailer can still enqueue a frame.
        if thread and thread.is_alive():
            _listener_thread = thread
            _listener_stop = stop_event
        else:
            _listener_thread = None
            _listener_stop = None


def _runtime_file_loop(
    paths: tuple[Path, ...],
    stop_event: threading.Event,
) -> None:
    global _runtime_file_native
    try:
        with PathSetChangeWaiter(
            paths,
            fallback_poll_seconds=RUNTIME_FILE_POLL_SECONDS,
        ) as changes:
            while not stop_event.is_set():
                _runtime_file_native = changes.native_notifications
                wait_seconds = (
                    60.0
                    if changes.native_notifications
                    else RUNTIME_FILE_POLL_SECONDS
                )
                if changes.wait(wait_seconds, stop_event):
                    notify_runtime_activity()
    finally:
        _runtime_file_native = False


def start_runtime_file_watch(
    paths: (
        str
        | os.PathLike[str]
        | list[str | os.PathLike[str]]
        | tuple[str | os.PathLike[str], ...]
    ),
) -> None:
    """Wake the runtime when any cross-process coordination file changes."""
    global _runtime_file_thread, _runtime_file_stop, _runtime_file_paths
    values = (
        [paths]
        if isinstance(paths, (str, os.PathLike))
        else list(paths)
    )
    resolved = tuple(
        sorted(
            {
                str(Path(path).expanduser().resolve())
                for path in values
            }
        )
    )
    if not resolved:
        raise ValueError("At least one runtime coordination file is required.")
    with _runtime_file_lock:
        if (
            _runtime_file_thread
            and _runtime_file_thread.is_alive()
            and _runtime_file_paths == resolved
        ):
            return
        if _runtime_file_thread and _runtime_file_thread.is_alive():
            return
        for path in resolved:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        _runtime_file_paths = resolved
        _runtime_file_stop = threading.Event()
        _runtime_file_thread = threading.Thread(
            target=_runtime_file_loop,
            args=(
                tuple(Path(path) for path in resolved),
                _runtime_file_stop,
            ),
            name="runtime-file-watch",
            daemon=True,
        )
        _runtime_file_thread.start()


def runtime_file_notifications_active() -> bool:
    thread = _runtime_file_thread
    return bool(
        thread
        and thread.is_alive()
        and _runtime_file_native
    )


def stop_runtime_file_watch() -> None:
    global _runtime_file_thread, _runtime_file_stop, _runtime_file_paths
    with _runtime_file_lock:
        stop_event = _runtime_file_stop
        thread = _runtime_file_thread
        if stop_event:
            stop_event.set()
        if thread and thread.is_alive():
            thread.join(timeout=2)
        if not thread or not thread.is_alive():
            _runtime_file_thread = None
            _runtime_file_stop = None
            _runtime_file_paths = ()


def maintenance_inbox_quiescent() -> bool:
    """True after every locally claimed durable frame has been committed."""
    with _listener_lock:
        listener_running = bool(
            _listener_thread and _listener_thread.is_alive()
        )
    with _inbox_retry_lock:
        retry_empty = not _inbox_retry_records
    return not listener_running and retry_empty and _event_queue.empty()


def _drain_listener_events(max_events: int = 500) -> list[InboxRecord]:
    records: list[InboxRecord] = []
    with _inbox_retry_lock:
        while _inbox_retry_records and len(records) < max_events:
            records.append(_inbox_retry_records.popleft())
    for _ in range(max_events):
        if len(records) >= max_events:
            break
        try:
            item = _event_queue.get_nowait()
            records.append(item if isinstance(item, InboxRecord) else InboxRecord(item))
        except queue.Empty:
            break
    if not _event_queue.empty():
        notify_runtime_activity()
    return records


def _retry_inbox_batch(records: list[InboxRecord]) -> None:
    """Put an uncommitted suffix ahead of newer frames for ordered replay."""
    if not records:
        return
    with _inbox_retry_lock:
        for record in reversed(records):
            _inbox_retry_records.appendleft(record)
    notify_runtime_activity()


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


def _events_from_durable_frame(frame: dict[str, Any]) -> list[dict[str, Any]]:
    event = _event_from_frame(frame)
    if event is not None:
        return [event]
    if frame.get("type") != "initial.snapshot":
        return []

    events: list[dict[str, Any]] = []
    for room in frame.get("rooms") or []:
        if not isinstance(room, dict):
            continue
        room_id = _room_id(room)
        timeline = room.get("timeline")
        if not isinstance(timeline, dict):
            continue
        for raw in timeline.get("events") or []:
            if not isinstance(raw, dict):
                continue
            payload = dict(raw)
            if room_id and not payload.get("room_id") and not payload.get("roomId"):
                payload["room_id"] = room_id
            events.append(payload)
    return events


@_state_serialized
def _remove_room_mapping(room_id: str) -> None:
    if not room_id:
        return
    state = _load_state()
    contact_id = state.setdefault("rooms", {}).pop(room_id, "")
    contact = state.setdefault("contacts", {}).get(contact_id) if contact_id else None
    if contact and contact.get("room_id") == room_id:
        contact["room_id"] = ""
        contact["updated_at"] = _utc_iso()
    _save_state(state)


def _schedule_room_refresh(client: InterfaceClient) -> None:
    """Collapse bursts of room invalidations into one background refresh."""
    submit_best_effort(
        discover_rooms,
        client,
        force=True,
        key="interface:room-refresh",
        coalesce=True,
    )


def _reconcile_durable_frame(frame: dict[str, Any], client: InterfaceClient) -> None:
    """Apply non-message stream state before interpreting following events."""
    frame_type = str(frame.get("type") or "")
    if frame_type == "_invalid_inbox_line":
        print("[Interface] skipped one malformed durable inbox line", flush=True)
        return
    if frame_type == "central_carbon_set":
        _schedule_room_refresh(client)
        return
    if frame_type in {"room.added", "room.updated"}:
        _schedule_room_refresh(client)
        return
    if frame_type == "room.removed":
        _remove_room_mapping(str(frame.get("room_id") or ""))
        return
    if frame_type == "initial.snapshot":
        # The snapshot is already barrier-consistent. Refreshing the compact
        # local contact projection makes its timeline events routable.
        _schedule_room_refresh(client)
        return
    if frame_type != "account.state":
        return

    kind = str(frame.get("kind") or "")
    room_id = str(frame.get("room_id") or "")
    data = frame.get("data")
    if isinstance(data, dict):
        room_id = room_id or str(data.get("room_id") or "")
    if kind == "room.remove":
        _remove_room_mapping(room_id)
    elif kind == "room.upsert":
        _schedule_room_refresh(client)


def get_unread_events(*, durable_handoff: bool = False) -> dict[str, str]:
    """Consume committed CLI v2 inbox records into manager contexts."""
    try:
        from interface.work_updates import replay_pending_call_updates

        replay_pending_call_updates()
    except Exception as exc:
        print(f"[Work updates] call retry scheduling failed: {exc}", flush=True)
    client = InterfaceClient()
    try:
        discover_rooms(client)
    except InterfaceError as exc:
        print(f"[Interface] {exc}", flush=True)
    except Exception as exc:
        print(f"[Interface] room discovery failed: {exc}", flush=True)

    try:
        from manager.runtime.maintenance import accepting_new_roots

        if accepting_new_roots():
            start_listener()
    except Exception:
        start_listener()
    contexts: dict[str, list[str]] = {}
    records = _drain_listener_events()
    for index, record in enumerate(records):
        retry_record = False
        try:
            _reconcile_durable_frame(record.frame, client)
            for event_index, event in enumerate(
                _events_from_durable_frame(record.frame)
            ):
                try:
                    processed = process_incoming_event(
                        event,
                        client=client,
                        defer_processed_watermark=durable_handoff,
                    )
                    if processed and durable_handoff:
                        contact_id, context = processed
                        room_id = _event_room_id(event)
                        event_id = _event_id(event)
                        if event_id:
                            ingress_id = f"interface:{room_id}:{event_id}"
                        elif record.file_id and record.end_offset > 0:
                            ingress_id = (
                                f"interface-record:{record.file_id}:"
                                f"{record.end_offset}:{event_index}"
                            )
                        else:
                            event_digest = hashlib.sha256(
                                json.dumps(
                                    event,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                    default=str,
                                ).encode("utf-8")
                            ).hexdigest()
                            ingress_id = f"interface-event:{event_digest}"
                        try:
                            from manager.runtime.maintenance import COORDINATOR

                            accepted = COORDINATOR.enqueue_ingress_root(
                                contact_id,
                                context,
                                ingress_id=ingress_id,
                            )
                            if not accepted:
                                raise DurableHandoffError(
                                    "Manager-root ownership was not accepted."
                                )
                            _remember_processed(
                                contact_id,
                                event_id,
                                room_id,
                            )
                        except DurableHandoffError:
                            raise
                        except Exception as exc:
                            raise DurableHandoffError(
                                "Manager-root ownership was not confirmed."
                            ) from exc
                except (CallBookkeepingError, DurableHandoffError):
                    retry_record = True
                    print(
                        "[Interface] durable event handoff deferred",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    print(
                        f"[Interface] durable event processing failed: {exc}",
                        flush=True,
                    )
                    continue
                if not processed:
                    continue
                contact_id, context = processed
                if not durable_handoff:
                    contexts.setdefault(contact_id, []).append(context)
        except Exception as exc:
            print(f"[Interface] durable frame processing failed: {exc}", flush=True)
        if retry_record:
            # Committing any later line would also acknowledge this one because
            # the cursor is an offset. Keep the whole suffix ahead of new work.
            _retry_inbox_batch(records[index:])
            break
        # A single malformed/unsupported frame must not poison the durable
        # stream and prevent every later room from being dispatched.
        _commit_inbox_record(record)

    return {contact_id: "\n---\n".join(parts) for contact_id, parts in contexts.items() if parts}


def get_unread_events_durable() -> dict[str, str]:
    """Transfer unread events to durable roots before committing the inbox."""
    return get_unread_events(durable_handoff=True)


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


def deliver_maintenance_notices(*, limit: int = 20) -> int:
    """Deliver durable, non-LLM maintenance acknowledgements to Carbons.

    The maintenance coordinator stores only one acknowledgement per contact
    and update.  A failed Interface call releases the claim for retry.
    """
    from manager.runtime.maintenance import COORDINATOR

    delivered = 0
    client = InterfaceClient()
    for notice in COORDINATOR.claim_notices(limit=limit):
        success = False
        try:
            contact, error = _contact_room_or_error(notice["contact_id"])
            if error or contact is None:
                raise InterfaceError(error or "maintenance contact is unavailable")
            client.send(
                str(contact["room_id"]),
                f"Silicon status: {notice['message']}",
            )
            success = True
            delivered += 1
        except Exception:
            success = False
        finally:
            COORDINATOR.finish_notice(
                notice["notice_id"],
                notice["claim_token"],
                delivered=success,
            )
    return delivered


def schedule_maintenance_notices() -> bool:
    """Retry durable Carbon acknowledgements without blocking the drain."""
    global _maintenance_notice_running
    with _maintenance_notice_lock:
        if _maintenance_notice_running:
            return False
        _maintenance_notice_running = True

    def run():
        global _maintenance_notice_running
        try:
            deliver_maintenance_notices()
        finally:
            with _maintenance_notice_lock:
                _maintenance_notice_running = False

    threading.Thread(
        target=run,
        name="maintenance-carbon-notices",
        daemon=True,
    ).start()
    return True


def _sent_event_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    nested = payload.get("event")
    if isinstance(nested, dict):
        value = _event_id(nested)
        if value:
            return value
    return _first_text(payload.get("event_id"), payload.get("eventId"), payload.get("id"))


def _reply_segment_client_id(
    client_id: str,
    *,
    index: int,
    count: int,
    segment_type: str,
) -> str:
    """Derive a stable bounded identity for each parsed reply segment."""
    client_id = str(client_id or "").strip()
    if not client_id:
        return ""
    if count <= 1:
        return client_id[:128]
    suffix = f":segment:{index + 1}:{segment_type}"
    return f"{client_id[: max(1, 128 - len(suffix))]}{suffix}"[:128]


def _record_sent_call_message(
    contact_id: str,
    message: str,
    event_id: str,
    *,
    terminal: bool = True,
) -> None:
    if not event_id or not message:
        return
    try:
        from interface.work_updates import (
            enqueue_outbound_call,
            prepare_outbound_call,
            record_contact_call_message,
        )

        own_profile = get_own_profile()
        idempotency_key = f"outgoing-call:{contact_id}:{event_id}"
        appended = record_contact_call_message(
            contact_id,
            speaker_kind="manager",
            speaker_id=str(own_profile.get("silicon_id") or "local-silicon"),
            speaker_name=str(own_profile.get("name") or "Silicon manager"),
            message=message,
            idempotency_key=idempotency_key,
            terminal=terminal,
        )
        if not appended:
            contact = get_contact(contact_id) or {}
            target_name = str(
                contact.get("display_name")
                or contact.get("name")
                or contact_id
            )
            reference = prepare_outbound_call(
                contact_id,
                target_kind="silicon",
                target_id=str(contact.get("silicon_id") or contact_id),
                target_name=target_name,
                message=message,
            )
            if not enqueue_outbound_call(
                reference,
                target_name=target_name,
                message=message,
                idempotency_key=idempotency_key,
            ):
                raise RuntimeError("Outgoing call intent was not accepted.")
    except Exception as exc:
        raise CallBookkeepingError(
            "Outgoing call bookkeeping was not durably committed."
        ) from exc


def reply_contact(
    message: str,
    contact_id: str,
    *,
    work_continues: bool = False,
    progress_group_id: str = "",
    client_id: str = "",
) -> str:
    contact, err = _contact_room_or_error(contact_id)
    if err:
        return err
    assert contact is not None
    client = InterfaceClient()
    room_id = contact["room_id"]
    if not progress_group_id:
        try:
            from interface.work_updates import current_manager_activity_group

            progress_group_id = current_manager_activity_group(contact_id)
        except Exception:
            progress_group_id = ""
    errors: list[str] = []
    try:
        from diagnostics.store import Diagnostics
        trace = Diagnostics.get_active_run(contact_id)
    except Exception:
        trace = None
    segments = _parse_reply_segments(message)
    final_text_index = max(
        (
            index
            for index, (segment_type, value) in enumerate(segments)
            if segment_type == "text" and value
        ),
        default=-1,
    )
    for segment_index, (seg_type, seg_value) in enumerate(segments):
        segment_client_id = _reply_segment_client_id(
            client_id,
            index=segment_index,
            count=len(segments),
            segment_type=seg_type,
        )
        try:
            span_ctx = trace.span("interface.reply_delivery") if trace is not None else None
            if span_ctx is not None:
                span_ctx.__enter__()
                span_ctx.set_meta(segment_type=seg_type, room_id=room_id)
            sent = None
            try:
                if seg_type == "text":
                    if seg_value:
                        sent = client.send(
                            room_id,
                            seg_value,
                            progress_group_id=progress_group_id,
                            work_continues=work_continues,
                            client_id=segment_client_id,
                        )
                elif seg_type == "file":
                    path = os.path.abspath(os.path.expanduser(seg_value.strip()))
                    if not os.path.exists(path):
                        errors.append(f"File not found: {path}")
                        continue
                    sent = client.send_file(room_id, path)
                    try:
                        from diagnostics.activity import attachment, url_from
                        attachment("sent", contact_id, url=url_from(sent), path=path,
                                   filename=os.path.basename(path))
                    except Exception:
                        pass
                elif seg_type == "voice":
                    sent = client.tts(room_id, seg_value)
                sent_event_id = _sent_event_id(sent)
                if sent is not None:
                    # `iwantto see` reads from this record. Glass reports read
                    # receipts outward only, so what this Silicon sent is only
                    # knowable if it is written down here as it goes out.
                    try:
                        from diagnostics.iwantto.journal import record_message
                        from diagnostics.iwantto.message_log import record_outbound

                        record_outbound(
                            contact_id, sent_event_id, seg_value, seg_type
                        )
                        record_message(
                            "out",
                            contact_id,
                            via="interface",
                            event_id=sent_event_id,
                            body=seg_value,
                        )
                    except Exception:
                        pass
                if (
                    seg_type == "text"
                    and contact.get("contact_type") == "silicon"
                    and sent_event_id
                ):
                    try:
                        _record_sent_call_message(
                            contact_id,
                            seg_value,
                            sent_event_id,
                            terminal=segment_index == final_text_index,
                        )
                    except CallBookkeepingError:
                        # The CLI durable inbox will replay our accepted self
                        # event with the same event-derived idempotency key.
                        print(
                            "[Interface] outgoing call bookkeeping deferred",
                            flush=True,
                        )
                if trace is not None and sent_event_id:
                    trace.add_response(
                        sent_event_id,
                        recipient_type=str(contact.get("contact_type") or "carbon"),
                        recipient_id=str(
                            contact.get("silicon_id")
                            or contact.get("carbon_id")
                            or contact.get("fixed_id")
                            or contact_id
                        ),
                        room_id=str(room_id),
                        accepted_by="glass",
                    )
                    if span_ctx is not None:
                        span_ctx.set_meta(response_event_id=sent_event_id)
            finally:
                if span_ctx is not None:
                    span_ctx.__exit__(None, None, None)
        except Exception as exc:
            errors.append(f"{seg_type} segment failed: {exc}")
    status = "Sent with errors: " + "; ".join(errors) if errors else "Message sent"
    try:
        from diagnostics.activity import reply as _log_reply
        _log_reply(contact_id, message, status)
    except Exception:
        pass
    return status


def send_progress(
    contact_id: str,
    group: str,
    state: str,
    message: str = "",
    *,
    frame_key: str = "",
    frame_id: str = "",
    revision: int | None = None,
    task_id: str = "",
    occurred_at: str = "",
    progress_pct: float | None = None,
    summary: str = "",
) -> None:
    contact = get_contact(contact_id)
    if not contact or not contact.get("room_id"):
        return
    try:
        from interface.work_updates import (
            activity_frame_identity,
            canonical_activity_state,
            current_manager_activity_group,
            touch_manager_call_activity,
        )

        group = group or current_manager_activity_group(contact_id)
        if not group:
            return
        touch_manager_call_activity(contact_id)
        state = canonical_activity_state(state)
        if not frame_id:
            fingerprint = json.dumps(
                {
                    "state": state,
                    "message": message,
                    "task_id": task_id,
                    "occurred_at": occurred_at,
                    "progress_pct": progress_pct,
                    "summary": summary,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            frame_id, accepted_revision, _ = activity_frame_identity(
                contact_id,
                group,
                frame_key=frame_key,
                fingerprint=fingerprint,
            )
            if revision is None:
                revision = accepted_revision
        submit_best_effort(
            _deliver_progress,
            contact_id,
            str(contact["room_id"]),
            group,
            state,
            message,
            frame_id,
            task_id,
            revision,
            occurred_at,
            progress_pct,
            summary,
            key=f"progress:{contact_id}:{group}:{frame_id}",
            coalesce=True,
        )
    except Exception as exc:
        _record_progress_failure(contact_id, group, state, exc)


def _deliver_progress(
    contact_id: str,
    room_id: str,
    group: str,
    state: str,
    message: str,
    frame_id: str,
    task_id: str,
    revision: int | None,
    occurred_at: str,
    progress_pct: float | None,
    summary: str,
) -> None:
    try:
        InterfaceClient().progress(
            room_id,
            group,
            state,
            message,
            frame_id=frame_id,
            task_id=task_id,
            revision=revision,
            occurred_at=occurred_at,
            progress_pct=progress_pct,
            summary=summary,
        )
    except Exception as exc:
        _record_progress_failure(contact_id, group, state, exc)


def _record_progress_failure(
    contact_id: str,
    group: str,
    state: str,
    exc: Exception,
) -> None:
    try:
        from diagnostics.store import Diagnostics

        trace = Diagnostics.get_active_run(contact_id)
        if trace is not None:
            trace.event(
                "interface.progress_failed",
                group_id=group,
                state=state,
                error=str(exc)[:500],
            )
    except Exception:
        pass


def parse_remote_browser_url(stdout: str) -> str:
    match = URL_RE.search(stdout or "")
    return match.group(0).rstrip(".,)") if match else ""


def _normalize_remote_browser_start_url(value: str | None) -> str:
    url = (value or "").strip() or REMOTE_BROWSER_START_URL
    if not url:
        return "https://www.google.com"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = f"https://{url}"
    if not url.lower().startswith(("http://", "https://")):
        return REMOTE_BROWSER_START_URL
    return url


def _remote_browser_cmd(session_name: str, profile: str, *parts: str) -> list[str]:
    return [
        "silicon-browser",
        "--session",
        session_name,
        "--profile",
        profile,
        *parts,
    ]


def _remote_browser_output(proc: subprocess.CompletedProcess[str]) -> str:
    return ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()


def _share_missing_session(output: str) -> bool:
    text = output.lower()
    return "no active session" in text or "open a page first" in text


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


def _save_remote_browser_event(session_name: str, event_id: str) -> None:
    try:
        def remember(state):
            if isinstance(state, dict):
                state[session_name] = event_id

        update_json(REMOTE_BROWSER_STATE_FILE, {}, remember)
    except Exception:
        pass


def _pop_remote_browser_event(session_name: str) -> str:
    event_id = ""
    try:
        def pop_event(state):
            nonlocal event_id
            if isinstance(state, dict):
                event_id = state.pop(session_name, "")

        update_json(REMOTE_BROWSER_STATE_FILE, {}, pop_event)
    except Exception:
        return ""
    return event_id if isinstance(event_id, str) else ""


def _remote_browser_lock_path(contact_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(contact_id or "contact"))[:80]
    return STATE_DIR / f"remote-browser-{safe_id}.json"


def remote_browser_share(contact_id: str, expiry: int = 60, new: bool = True, url: str = "") -> str:
    with file_lock(_remote_browser_lock_path(contact_id)):
        return _remote_browser_share_locked(contact_id, expiry=expiry, new=new, url=url)


def _remote_browser_share_locked(contact_id: str, expiry: int = 60, new: bool = True, url: str = "") -> str:
    contact, err = _contact_room_or_error(contact_id)
    if err:
        return err
    assert contact is not None

    from worker.handler import SILICON_BROWSER_PROFILE

    try:
        minutes = int(expiry or 60)
    except (TypeError, ValueError):
        minutes = 60
    if minutes <= 0:
        minutes = 60

    session_name = f"remote-{contact_id}"
    start_url = _normalize_remote_browser_start_url(url)

    def open_session() -> str:
        open_cmd = _remote_browser_cmd(
            session_name,
            SILICON_BROWSER_PROFILE,
            "open",
            start_url,
            "--timeout",
            str(minutes),
        )
        open_proc = subprocess.run(open_cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=180)
        if open_proc.returncode != 0:
            return _remote_browser_output(open_proc)
        return ""

    if new:
        close_cmd = _remote_browser_cmd(session_name, SILICON_BROWSER_PROFILE, "close")
        subprocess.run(close_cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
        open_error = open_session()
        if open_error:
            return f"Error: silicon-browser open failed: {open_error}"

    cmd = _remote_browser_cmd(
        session_name,
        SILICON_BROWSER_PROFILE,
        "share",
        "--expiry",
        str(minutes),
    )
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120)
    output = _remote_browser_output(proc)
    if proc.returncode != 0 and not new and _share_missing_session(output):
        open_error = open_session()
        if open_error:
            return f"Error: silicon-browser open failed: {open_error}"
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120)
        output = _remote_browser_output(proc)
    if proc.returncode != 0:
        return f"Error: silicon-browser share failed: {output}"
    url = parse_remote_browser_url(output)
    if not url:
        return f"Error: silicon-browser did not return a share URL: {output}"

    posted = InterfaceClient().remote_browser(contact["room_id"], url, minutes)
    event_id = _extract_event_id(posted)
    if event_id:
        _save_remote_browser_event(session_name, event_id)
    branded_url = _extract_remote_browser_url(posted, fallback=url)
    return f"Done. Remote browser shared. session={session_name}, expiry_minutes={minutes}, url={branded_url}"


def remote_browser_close(contact_id: str) -> str:
    with file_lock(_remote_browser_lock_path(contact_id)):
        return _remote_browser_close_locked(contact_id)


def _remote_browser_close_locked(contact_id: str) -> str:
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
            from interface.config import silicon_api_post

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
