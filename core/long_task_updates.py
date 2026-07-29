"""Durable runtime liveness for substantial manager work.

The model still owns useful task structure and milestone prose.  This module
is the runtime safety rail: it keeps one task identity stable, journals every
operation that must survive a process exit, and will not publish a final prose
reply until the corresponding terminal card is accepted by Glass.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from core.interface import STATE_DIR
from core.state_store import read_json, update_json
from core.work_updates import (
    active_task_id,
    execute_work_update,
    record_worker_started,
    record_worker_state,
    refresh_task_snapshot,
)


ACTIVITY_HEARTBEAT_SECONDS = max(
    1.0,
    float(os.environ.get("SILICON_ACTIVITY_HEARTBEAT_SECONDS", "12")),
)
DURABLE_HEARTBEAT_SECONDS = max(
    ACTIVITY_HEARTBEAT_SECONDS,
    float(os.environ.get("SILICON_DURABLE_HEARTBEAT_SECONDS", "90")),
)
RETRY_MAX_SECONDS = 60.0
LEASE_SECONDS = max(
    5.0,
    float(os.environ.get("SILICON_LONG_TASK_LEASE_SECONDS", "30")),
)
MAX_RECOVERY_CONTACTS = 64
MAX_ACTIVE_CONTACTS = 128
MAX_STATE_CONTACTS = 256
MAX_ALIASES = 64
MAX_PENDING_WORKERS = 64
MAX_PENDING_REPLY_CHARS = 262_144
MAX_PENDING_REPLY_ATTEMPTS = 12
PREPARED_RECONCILE_GRACE_SECONDS = 120.0
MAX_QUEUED_ROOTS = 128
MAX_QUEUED_ROOTS_PER_CONTACT = 16
QUEUED_ROOT_LEASE_SECONDS = 60.0
ACCURACY_REVIEW_SEGMENTS = 20
ACCURACY_REVIEW_CLAIM_SECONDS = 60.0
MAX_ACCURACY_REVIEW_CONTEXT_CHARS = 32_768
STALE_ACTIVE_SECONDS = 30 * 24 * 60 * 60
TOMBSTONE_SECONDS = 7 * 24 * 60 * 60
LONG_TASK_STATE_FILE = Path(STATE_DIR) / "long_task_updates.json"

_PROCESS_TOKEN = f"{os.getpid()}:{uuid.uuid4().hex}"
_REGISTRY_LOCK = threading.RLock()
_ACTIVE_BY_CONTACT: dict[str, "LongTaskLifecycle"] = {}

_SAFE_ACTIVITY_NOTES = {
    "reading_file": "Reviewing the relevant material",
    "writing_file": "Applying the current changes",
    "executing": "Running the current step",
    "searching_web": "Researching the current step",
    "thinking": "Working through the next step",
    "spawning_worker": "Workers are processing the request",
    "continuing": "Continuing with the next step",
    "working": "Work is still in progress",
}
_TERMINAL_ACTIONS = {"task/complete", "task/fail", "task/cancel"}
_TERMINAL_STATES = {"completed", "failed", "cancelled"}
_QUEUED_ROOT_MARKER = "durable_queued_root_id:"
_QUEUED_ROOT_VISIBILITY_MARKER = "durable_queued_root_visible:"
_ACCURACY_REVIEW_MARKER = "durable_accuracy_review_id:"


def _default_state() -> dict[str, Any]:
    return {"version": 2, "contacts": {}, "queued_roots": {}}


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _compact(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _terminal_reply_delivery_status(status: Any) -> bool:
    """Return true when replaying the same reply can never succeed."""
    text = str(status or "")
    if "idempotency_conflict" in text.lower():
        return True
    match = re.search(
        r"\b(?:HTTP|api)\s+([1-5][0-9]{2})\b",
        text,
        flags=re.IGNORECASE,
    )
    return bool(
        match
        and int(match.group(1))
        in {400, 404, 405, 409, 410, 413, 422}
    )


def _non_negative_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number <= 0:
        return 0.0
    return number


def _estimate_goal_from_data(data: Any) -> tuple[bool, float]:
    """Return estimate-field presence and its accepted displayed goal."""
    if not isinstance(data, dict):
        return False, 0.0
    timing = data.get("timing")
    sources = [data, timing] if isinstance(timing, dict) else [data]
    for source in sources:
        if "estimate_seconds" not in source:
            continue
        accepted = _non_negative_number(source.get("estimate_seconds"))
        return True, float(math.ceil(accepted)) if accepted else 0.0
    for source in sources:
        if "realistic_estimate_seconds" not in source:
            continue
        realistic = _non_negative_number(
            source.get("realistic_estimate_seconds")
        )
        if realistic:
            # This is the silicon-interface CLI's accepted transformation.
            return True, float(math.ceil(realistic * 1.05))
        return True, 0.0
    return False, 0.0


def _goal_seconds_from_data(data: Any) -> float:
    """Return the exact displayed goal implied by manager estimate input."""
    return _estimate_goal_from_data(data)[1]


def _goal_materially_changed(previous: float, current: float) -> bool:
    previous = _non_negative_number(previous)
    current = _non_negative_number(current)
    if not previous or not current:
        return bool(previous) != bool(current)
    return abs(current - previous) >= max(1.0, previous * 0.01)


def _title_from_context(context: str) -> str:
    text = str(context or "")
    match = re.search(r"(?:^|\n)message:\s*(.*)", text, flags=re.S | re.I)
    body = match.group(1) if match else text
    ignored_prefixes = (
        "event_id:",
        "room_id:",
        "sender",
        "timestamp:",
        "attachment",
    )
    for raw_line in body.splitlines():
        line = " ".join(raw_line.split()).strip(" -")
        if not line or line.lower().startswith(ignored_prefixes):
            continue
        return line if len(line) <= 76 else line[:75].rstrip() + "…"
    return "Working on your request"


def _successful(result: Any) -> bool:
    return str(result or "").startswith("Done.")


def _retry_at(attempts: int) -> float:
    return time.time() + min(2 ** min(max(1, attempts), 6), RETRY_MAX_SECONDS)


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _bounded_mapping(value: Any, limit: int = MAX_ALIASES) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    items = list(value.items())[-limit:]
    return {
        _compact(key, 256): _compact(mapped, 256)
        for key, mapped in items
        if key and mapped
    }


def _tombstone(entry: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = float(now or time.time())
    return {
        "active": False,
        "run_fingerprint": _fingerprint(entry.get("run_id")),
        "task_fingerprint": _fingerprint(entry.get("task_id")),
        "settled_at": now,
        "updated_at": now,
    }


def _prune_state_locked(state: dict[str, Any], now: float | None = None) -> None:
    """Bound retained state without discarding recent live delivery intent."""
    now = float(now or time.time())
    state["version"] = 2
    contacts = state.setdefault("contacts", {})
    if not isinstance(contacts, dict):
        state["contacts"] = {}
        return

    for contact_id, raw in list(contacts.items()):
        if not isinstance(raw, dict):
            contacts.pop(contact_id, None)
            continue
        updated = float(raw.get("updated_at") or 0)
        if raw.get("active") and updated and now - updated > STALE_ACTIVE_SECONDS:
            contacts[contact_id] = _tombstone(raw, now)
            continue
        if not raw.get("active"):
            # Old versions retained titles/descriptions in inactive entries.
            if set(raw) - {
                "active",
                "run_fingerprint",
                "task_fingerprint",
                "settled_at",
                "updated_at",
            }:
                raw = contacts[contact_id] = _tombstone(raw, now)
            settled = float(raw.get("settled_at") or raw.get("updated_at") or 0)
            if settled and now - settled > TOMBSTONE_SECONDS:
                contacts.pop(contact_id, None)

    if len(contacts) <= MAX_STATE_CONTACTS:
        pass
    else:
        inactive = sorted(
            (
                (float(raw.get("updated_at") or 0), contact_id)
                for contact_id, raw in contacts.items()
                if isinstance(raw, dict) and not raw.get("active")
            )
        )
        for _, contact_id in inactive:
            if len(contacts) <= MAX_STATE_CONTACTS:
                break
            contacts.pop(contact_id, None)

    queued = state.setdefault("queued_roots", {})
    if not isinstance(queued, dict):
        state["queued_roots"] = {}
        return
    for contact_id, items in list(queued.items()):
        if not isinstance(items, list):
            queued.pop(contact_id, None)
            continue
        valid = [item for item in items if isinstance(item, dict)]
        if valid:
            # Never prune an accepted root. New roots are rejected before
            # crossing the bound so their maintenance admission can retry.
            queued[contact_id] = valid
        else:
            queued.pop(contact_id, None)


def _state_entry(contact_id: str) -> dict[str, Any]:
    state = read_json(LONG_TASK_STATE_FILE, _default_state())
    contacts = state.get("contacts") if isinstance(state, dict) else {}
    entry = contacts.get(str(contact_id)) if isinstance(contacts, dict) else {}
    return deepcopy(entry) if isinstance(entry, dict) else {}


def _active_entries() -> list[tuple[str, dict[str, Any]]]:
    state = read_json(LONG_TASK_STATE_FILE, _default_state())
    contacts = state.get("contacts") if isinstance(state, dict) else {}
    if not isinstance(contacts, dict):
        return []
    entries = [
        (str(contact_id), deepcopy(entry))
        for contact_id, entry in contacts.items()
        if isinstance(entry, dict) and entry.get("active")
    ]
    entries.sort(
        key=lambda item: float(item[1].get("updated_at") or 0),
        reverse=True,
    )
    return entries


def _queued_root_id(contact_id: str, run_id: str, context: str) -> str:
    return _stable_id("queued-root", contact_id, run_id, context)


def queue_long_task_root_if_blocked(
    contact_id: str,
    run_id: str,
    context: str,
    *,
    visible: bool,
) -> bool:
    """Durably defer an unrelated root while terminal delivery is fenced."""
    contact_id = str(contact_id)
    run_id = str(run_id or _stable_id("run", contact_id, context))
    lifecycle = current_long_task(contact_id)
    if lifecycle is not None:
        with lifecycle._lock:
            blocked = (
                run_id != lifecycle.run_id
                and bool(
                    lifecycle.pending_reply
                    or lifecycle._settle_requested
                    or lifecycle._terminal
                )
            )
    else:
        entry = _state_entry(contact_id)
        blocked = bool(
            entry.get("active")
            and run_id != str(entry.get("run_id") or "")
            and (
                entry.get("pending_reply")
                or entry.get("settle_requested")
                or entry.get("terminal")
            )
        )
    if not blocked:
        return False

    root_id = _queued_root_id(contact_id, run_id, context)
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        _prune_state_locked(state, now)
        queued = state.setdefault("queued_roots", {})
        items = queued.setdefault(contact_id, [])
        if any(item.get("root_id") == root_id for item in items):
            return
        total = sum(
            len(value)
            for value in queued.values()
            if isinstance(value, list)
        )
        if (
            len(items) >= MAX_QUEUED_ROOTS_PER_CONTACT
            or total >= MAX_QUEUED_ROOTS
        ):
            # The caller is running under ManagerDispatcher's durable root
            # admission. Failing closed makes that admission retry instead of
            # silently dropping or attaching this unrelated request.
            raise RuntimeError("durable long-task root queue is at capacity")
        item = {
            "root_id": root_id,
            "run_id": run_id,
            "context": str(context),
            "visible": bool(visible),
            "created_at": now,
            "claim_owner": "",
            "claim_pid": 0,
            "claim_until": 0.0,
        }
        items.append(item)

    update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)
    return True


def claim_ready_long_task_roots(
    *,
    limit: int = 16,
) -> dict[str, str]:
    """Claim one queued root per idle contact for the local dispatcher."""
    claimed: dict[str, str] = {}
    owner = _PROCESS_TOKEN
    now = time.time()
    bounded_limit = max(0, min(int(limit), 64))

    def mutate(state: dict[str, Any]) -> None:
        _prune_state_locked(state, now)
        contacts = state.setdefault("contacts", {})
        queued = state.setdefault("queued_roots", {})
        for contact_id, items in sorted(
            queued.items(),
            key=lambda pair: float(
                (pair[1][0] if pair[1] else {}).get("created_at") or 0
            ),
        ):
            if len(claimed) >= bounded_limit:
                break
            entry = contacts.get(contact_id)
            if isinstance(entry, dict) and entry.get("active"):
                continue
            if not isinstance(items, list) or not items:
                continue
            item = items[0]
            claim_owner = str(item.get("claim_owner") or "")
            claim_until = float(item.get("claim_until") or 0)
            if (
                claim_owner
                and claim_until > now
                and _pid_alive(item.get("claim_pid"))
            ):
                continue
            item["claim_owner"] = owner
            item["claim_pid"] = os.getpid()
            item["claim_until"] = now + QUEUED_ROOT_LEASE_SECONDS
            claimed[str(contact_id)] = (
                f"{_QUEUED_ROOT_MARKER} {item['root_id']}\n"
                f"{_QUEUED_ROOT_VISIBILITY_MARKER} "
                f"{1 if item.get('visible', True) else 0}\n"
                f"{str(item.get('context') or '')}"
            )

    update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)
    return claimed


def extract_queued_long_task_root(context: str) -> tuple[str, str]:
    """Remove dispatcher-only durable metadata before invoking the manager."""
    root_id, clean_context, _ = extract_queued_long_task_root_metadata(
        context
    )
    return root_id, clean_context


def extract_queued_long_task_root_metadata(
    context: str,
) -> tuple[str, str, bool | None]:
    """Extract a queued root and its durable visibility decision."""
    text = str(context or "")
    first, separator, rest = text.partition("\n")
    if not first.startswith(_QUEUED_ROOT_MARKER):
        return "", text, None
    root_id = first.removeprefix(_QUEUED_ROOT_MARKER).strip()
    clean_context = rest if separator else ""
    visibility: bool | None = None
    visibility_line, visibility_separator, remainder = (
        clean_context.partition("\n")
    )
    if visibility_line.startswith(_QUEUED_ROOT_VISIBILITY_MARKER):
        encoded = visibility_line.removeprefix(
            _QUEUED_ROOT_VISIBILITY_MARKER
        ).strip()
        if encoded in {"0", "1"}:
            visibility = encoded == "1"
        clean_context = remainder if visibility_separator else ""
    return root_id, clean_context, visibility


def extract_accuracy_review_root(context: str) -> tuple[str, str]:
    """Remove the internal accuracy-review delivery marker."""
    text = str(context or "")
    first, separator, rest = text.partition("\n")
    if not first.startswith(_ACCURACY_REVIEW_MARKER):
        return "", text
    return (
        first.removeprefix(_ACCURACY_REVIEW_MARKER).strip(),
        rest if separator else "",
    )


def acknowledge_queued_long_task_root(root_id: str) -> None:
    root_id = str(root_id or "")
    if not root_id:
        return

    def mutate(state: dict[str, Any]) -> None:
        queued = state.setdefault("queued_roots", {})
        for contact_id, items in list(queued.items()):
            if not isinstance(items, list):
                continue
            remaining = [
                item
                for item in items
                if not (
                    isinstance(item, dict)
                    and item.get("root_id") == root_id
                )
            ]
            if remaining:
                queued[contact_id] = remaining
            else:
                queued.pop(contact_id, None)

    update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)


def _claim_contact(
    contact_id: str,
    owner: str,
    *,
    expected_run_id: str = "",
    allow_create: bool,
) -> dict[str, Any] | None:
    claimed: dict[str, Any] | None = None
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        nonlocal claimed
        _prune_state_locked(state, now)
        contacts = state.setdefault("contacts", {})
        entry = contacts.get(str(contact_id))
        if not isinstance(entry, dict) or not entry.get("active"):
            if not allow_create:
                return
            active_count = sum(
                1
                for item in contacts.values()
                if isinstance(item, dict) and item.get("active")
            )
            if active_count >= MAX_ACTIVE_CONTACTS:
                return
            entry = {
                "active": True,
                "contact_id": str(contact_id),
                "run_id": str(expected_run_id),
                "updated_at": now,
            }
            contacts[str(contact_id)] = entry
        lease_owner = str(entry.get("lease_owner") or "")
        lease_until = float(entry.get("lease_until") or 0)
        lease_pid = entry.get("lease_pid")
        if (
            lease_owner
            and lease_owner != owner
            and lease_until > now
            and _pid_alive(lease_pid)
        ):
            return
        entry["lease_owner"] = owner
        entry["lease_pid"] = os.getpid()
        entry["lease_until"] = now + LEASE_SECONDS
        entry["updated_at"] = now
        claimed = deepcopy(entry)

    update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)
    return claimed


class LongTaskLifecycle:
    """One contact's current durable task and delivery barrier."""

    def __init__(
        self,
        contact_id: str,
        run_id: str,
        context: str,
        *,
        activity_heartbeat: Callable[[str], None] | None = None,
        activity_heartbeat_seconds: float = ACTIVITY_HEARTBEAT_SECONDS,
        durable_heartbeat_seconds: float = DURABLE_HEARTBEAT_SECONDS,
        saved: dict[str, Any] | None = None,
        auto_start: bool = True,
        lease_owner: str = "",
        recovery: bool = False,
        reply_sender: Callable[..., str] | None = None,
        has_active_workers: Callable[[str], bool] | None = None,
        worker_status_resolver: Callable[[str, str], str] | None = None,
    ):
        self.contact_id = str(contact_id)
        self.run_id = str(run_id or _stable_id("run", contact_id, context))
        self.started_at = time.time()
        self.activity_heartbeat_seconds = max(
            0.1, float(activity_heartbeat_seconds)
        )
        self.durable_heartbeat_seconds = max(
            self.activity_heartbeat_seconds,
            float(durable_heartbeat_seconds),
        )
        self.activity_heartbeat = activity_heartbeat
        self.reply_sender = reply_sender
        self.has_active_workers = has_active_workers
        self.worker_status_resolver = worker_status_resolver
        self.title = _title_from_context(context)
        self.task_id = ""
        self.task_confirmed = False
        self.todo_id = ""
        self.base_description = (
            "Work is underway. This card stays current until the request is complete."
        )
        self.latest_activity = "Working through the request"
        self.task_aliases: dict[str, str] = {}
        self.todo_aliases: dict[str, str] = {}
        self.pending_workers: dict[str, dict[str, Any]] = {}
        self.worker_delivery_watermarks: dict[str, float] = {}
        self.pending_reply: dict[str, Any] = {}
        self.accuracy_schedule: dict[str, Any] = {}
        self._pending_create_spec: dict[str, Any] = {}
        self._desired_timer_state = "running"
        self._desired_pause_reason = ""
        self._timer_dirty = False
        self._timer_attempts = 0
        self._next_timer_attempt_at = 0.0
        self._blocker_resolution_pending = False
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._reply_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._manager_running = not recovery
        self._final_reply_sent = False
        self._deferred = False
        self._defer_pause_reason = "infrastructure"
        self._terminal = False
        self._model_create_started_at = 0.0
        self._runtime_create_inflight = False
        self._create_attempts = 0
        self._next_create_attempt_at = 0.0
        self._settle_attempts = 0
        self._next_settle_attempt_at = 0.0
        self._settle_requested = False
        self._heartbeat_attempts = 0
        self._next_heartbeat_attempt_at = 0.0
        self._last_activity_heartbeat_at = self.started_at
        self._last_durable_heartbeat_at = self.started_at
        self._last_durable_description = ""
        self._lease_owner = str(
            lease_owner or f"{_PROCESS_TOKEN}:{uuid.uuid4().hex}"
        )
        self._recovery_mode = bool(recovery)

        saved = saved if isinstance(saved, dict) else {}
        if saved.get("active"):
            self.run_id = str(saved.get("run_id") or self.run_id)
            self.started_at = float(saved.get("started_at") or self.started_at)
            self.task_id = str(saved.get("task_id") or "")
            self.task_confirmed = bool(saved.get("task_confirmed"))
            self.todo_id = str(saved.get("todo_id") or self.todo_id)
            self.base_description = _compact(
                saved.get("base_description") or self.base_description,
                1_500,
            )
            self.title = _compact(saved.get("title") or self.title, 120)
            self.latest_activity = _compact(
                saved.get("latest_activity") or self.latest_activity,
                200,
            )
            self.task_aliases = _bounded_mapping(saved.get("task_aliases"))
            self.todo_aliases = _bounded_mapping(saved.get("todo_aliases"))
            workers = saved.get("pending_workers")
            if isinstance(workers, dict):
                for worker_id, intent in list(workers.items())[
                    -MAX_PENDING_WORKERS:
                ]:
                    if isinstance(intent, dict):
                        self.pending_workers[str(worker_id)] = deepcopy(intent)
            watermarks = saved.get("worker_delivery_watermarks")
            if isinstance(watermarks, dict):
                self.worker_delivery_watermarks = {
                    str(worker_id): float(value or 0)
                    for worker_id, value in list(watermarks.items())[
                        -MAX_PENDING_WORKERS:
                    ]
                }
            pending_reply = saved.get("pending_reply")
            if isinstance(pending_reply, dict):
                self.pending_reply = deepcopy(pending_reply)
            accuracy_schedule = saved.get("accuracy_schedule")
            if isinstance(accuracy_schedule, dict):
                self.accuracy_schedule = deepcopy(accuracy_schedule)
            pending_create = saved.get("pending_create_spec")
            if isinstance(pending_create, dict):
                self._pending_create_spec = deepcopy(pending_create)
            self._create_attempts = int(saved.get("create_attempts") or 0)
            self._next_create_attempt_at = float(
                saved.get("next_create_attempt_at") or 0
            )
            self._settle_attempts = int(saved.get("settle_attempts") or 0)
            self._next_settle_attempt_at = float(
                saved.get("next_settle_attempt_at") or 0
            )
            self._settle_requested = bool(saved.get("settle_requested"))
            self._last_durable_description = str(
                saved.get("last_durable_description") or ""
            )
            self._heartbeat_attempts = int(
                saved.get("heartbeat_attempts") or 0
            )
            self._next_heartbeat_attempt_at = float(
                saved.get("next_heartbeat_attempt_at") or 0
            )
            self._defer_pause_reason = str(
                saved.get("defer_pause_reason") or "infrastructure"
            )
            self._deferred = bool(saved.get("deferred"))
            self._terminal = bool(saved.get("terminal"))
            self._desired_timer_state = str(
                saved.get("desired_timer_state") or "running"
            )
            self._desired_pause_reason = str(
                saved.get("desired_pause_reason") or ""
            )
            self._timer_dirty = bool(saved.get("timer_dirty"))
            self._timer_attempts = int(saved.get("timer_attempts") or 0)
            self._next_timer_attempt_at = float(
                saved.get("next_timer_attempt_at") or 0
            )
            self._blocker_resolution_pending = bool(
                saved.get("blocker_resolution_pending")
            )
            if (
                recovery
                and bool(saved.get("manager_running"))
                and not self.pending_reply
                and not self._terminal
                and not self._deferred
            ):
                # The manager process cannot resume a lost provider turn.  Keep
                # the card honest and recoverable instead of claiming progress.
                self._deferred = True
                self._defer_pause_reason = "infrastructure"
                self._desired_timer_state = "paused"
                self._desired_pause_reason = "infrastructure"
                self._timer_dirty = True

        claimed = _claim_contact(
            self.contact_id,
            self._lease_owner,
            expected_run_id=self.run_id,
            allow_create=not bool(saved.get("active")),
        )
        if claimed is None:
            self._closed = True
            self._stop.set()
        else:
            self._persist(active=True)
        if auto_start and not self._closed:
            self.start()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return not self._closed

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed:
                return
            self._thread = threading.Thread(
                target=self._watch,
                name=f"long-task-{self.contact_id[:24]}",
                daemon=True,
            )
            self._thread.start()

    def attach(
        self,
        run_id: str,
        context: str,
        activity_heartbeat: Callable[[str], None] | None,
    ) -> None:
        """Attach a continuation without overwriting unresolved durable intent."""
        with self._lock:
            if self._closed:
                return
            self._manager_running = True
            if self._defer_pause_reason != "blocker":
                self._deferred = False
                self._defer_pause_reason = "infrastructure"
                self._desired_timer_state = "running"
                self._desired_pause_reason = ""
                self._timer_dirty = bool(self.task_id)
                self._next_timer_attempt_at = 0.0
            if activity_heartbeat is not None:
                self.activity_heartbeat = activity_heartbeat
            if not self.title or self.title == "Working on your request":
                self.title = _title_from_context(context)
            self._persist(active=True)

    def observe(self, state: str) -> None:
        note = _SAFE_ACTIVITY_NOTES.get(
            str(state), _SAFE_ACTIVITY_NOTES["working"]
        )
        with self._lock:
            self.latest_activity = note

    def resolve_task_id(self, requested: str = "") -> str:
        with self._lock:
            requested = str(requested or "")
            return self.task_aliases.get(requested, requested or self.task_id)

    def _set_accuracy_goal_locked(
        self,
        task_id: str,
        goal_seconds: float,
        *,
        now: float | None = None,
    ) -> bool:
        task_id = str(task_id or "")
        goal_seconds = _non_negative_number(goal_seconds)
        if not task_id or not goal_seconds:
            return False
        existing = self.accuracy_schedule
        if (
            isinstance(existing, dict)
            and existing.get("task_id") == task_id
            and not _goal_materially_changed(
                float(existing.get("goal_seconds") or 0),
                goal_seconds,
            )
        ):
            return False
        now = float(now or time.time())
        generation = _stable_id(
            "accuracy-schedule",
            self.contact_id,
            task_id,
            goal_seconds,
            time.time_ns(),
        )
        self.accuracy_schedule = {
            "task_id": task_id,
            "goal_seconds": goal_seconds,
            "interval_seconds": goal_seconds / ACCURACY_REVIEW_SEGMENTS,
            "anchor_at": now,
            "next_checkpoint": 1,
            "generation": generation,
            "pending_review": {},
            "refresh_attempts": 0,
            "next_refresh_attempt_at": 0.0,
            "updated_at": now,
        }
        return True

    def _schedule_accuracy_from_data_locked(
        self,
        task_id: str,
        data: Any,
    ) -> bool:
        estimate_present, goal_seconds = _estimate_goal_from_data(data)
        if not goal_seconds:
            if (
                estimate_present
                and isinstance(self.accuracy_schedule, dict)
                and self.accuracy_schedule.get("task_id") == str(task_id)
            ):
                self._cancel_accuracy_schedule_locked()
                return True
            return False
        return self._set_accuracy_goal_locked(task_id, goal_seconds)

    def _cancel_accuracy_schedule_locked(self) -> None:
        self.accuracy_schedule = {}

    def _has_durable_delivery_locked(self) -> bool:
        return bool(
            self.pending_reply
            or self.pending_workers
            or self._pending_create_spec
            or self._runtime_create_inflight
            or self._settle_requested
        )

    def _discard_terminal_worker_updates_locked(self) -> bool:
        """Cancel card mutations made obsolete by an accepted terminal task."""
        removed = False
        now = time.time()
        for worker_id, intent in list(self.pending_workers.items()):
            if (
                not isinstance(intent, dict)
                or intent.get("phase") not in {"launched", "published"}
            ):
                continue
            self.pending_workers.pop(worker_id, None)
            self.worker_delivery_watermarks[str(worker_id)] = max(
                now,
                float(intent.get("fact_updated_at") or 0),
            )
            removed = True
        return removed

    def close_if_terminal(self) -> bool:
        """Tombstone and unregister a terminal lifecycle with no further turn."""
        with self._lock:
            if self._closed:
                return True
            if not self._terminal:
                return False
            removed_workers = self._discard_terminal_worker_updates_locked()
            if self._has_durable_delivery_locked():
                if removed_workers:
                    self._persist(active=True)
                return False
            self._close_locked()
        _unregister(self)
        return True

    def _accuracy_review_context(
        self,
        *,
        schedule: dict[str, Any],
        snapshot: dict[str, Any],
        checkpoint_from: int,
        checkpoint_through: int,
    ) -> str:
        task_id = str(schedule.get("task_id") or self.task_id)
        goal_seconds = float(schedule.get("goal_seconds") or 0)
        interval_seconds = float(schedule.get("interval_seconds") or 0)
        snapshot_json = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        context = (
            "Internal task accuracy review. This is not a user message and "
            "must not produce a normal reply.\n"
            f"task_id: {task_id}\n"
            f"accepted_goal_seconds: {goal_seconds:g}\n"
            f"review_interval_seconds: {interval_seconds:g}\n"
            f"checkpoint_from: {checkpoint_from}\n"
            f"checkpoint_through: {checkpoint_through}\n"
            "Inspect the accepted task, Todos, estimate, timer, blockers, "
            "workers, and current execution facts. Publish work_update "
            "mutations only where the durable card is materially inaccurate "
            "or stale. Otherwise use do_nothing. Do not call reply or contact "
            "another manager solely for this review.\n"
            f"accepted_task_snapshot: {snapshot_json}"
        )
        if len(context) > MAX_ACCURACY_REVIEW_CONTEXT_CHARS:
            context = (
                context[: MAX_ACCURACY_REVIEW_CONTEXT_CHARS - 1] + "…"
            )
        return context

    def _prepare_accuracy_review_if_due(self) -> bool:
        """Materialize at most one coalesced internal review root."""
        with self._io_lock:
            with self._lock:
                schedule = deepcopy(self.accuracy_schedule)
                if (
                    self._closed
                    or self._terminal
                    or not schedule
                    or schedule.get("pending_review")
                ):
                    return False
                now = time.time()
                if now < float(
                    schedule.get("next_refresh_attempt_at") or 0
                ):
                    return False
                interval = _non_negative_number(
                    schedule.get("interval_seconds")
                )
                anchor_at = float(schedule.get("anchor_at") or now)
                next_checkpoint = max(
                    1, int(schedule.get("next_checkpoint") or 1)
                )
                if (
                    not interval
                    or now < anchor_at + next_checkpoint * interval
                ):
                    return False
                task_id = str(schedule.get("task_id") or "")
                generation = str(schedule.get("generation") or "")

            snapshot = refresh_task_snapshot(self.contact_id, task_id)
            if str(snapshot.get("state") or "") in _TERMINAL_STATES:
                with self._lock:
                    current = self.accuracy_schedule
                    if (
                        self._closed
                        or not current
                        or current.get("generation") != generation
                        or current.get("pending_review")
                    ):
                        return False
                    self._terminal = True
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                self.close_if_terminal()
                return False
            with self._lock:
                current = self.accuracy_schedule
                if (
                    not current
                    or current.get("generation") != generation
                    or current.get("pending_review")
                    or self._terminal
                ):
                    return False
                if not snapshot:
                    attempts = int(current.get("refresh_attempts") or 0) + 1
                    current["refresh_attempts"] = attempts
                    current["next_refresh_attempt_at"] = _retry_at(attempts)
                    current["updated_at"] = time.time()
                    self._persist(active=True)
                    return False
                estimate_present, accepted_goal = _estimate_goal_from_data(
                    snapshot
                )
                if estimate_present and not accepted_goal:
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return False
                if accepted_goal and _goal_materially_changed(
                    float(current.get("goal_seconds") or 0),
                    accepted_goal,
                ):
                    self._set_accuracy_goal_locked(
                        task_id,
                        accepted_goal,
                        now=time.time(),
                    )
                    self._persist(active=True)
                    return False

                now = time.time()
                interval = _non_negative_number(
                    current.get("interval_seconds")
                )
                anchor_at = float(current.get("anchor_at") or now)
                checkpoint_from = max(
                    1, int(current.get("next_checkpoint") or 1)
                )
                if not interval:
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return False
                checkpoint_through = int(
                    math.floor(
                        max(0.0, now - anchor_at) / interval + 1e-9
                    )
                )
                if checkpoint_through < checkpoint_from:
                    return False
                review_id = _stable_id(
                    "accuracy-review",
                    current.get("generation"),
                    checkpoint_from,
                    checkpoint_through,
                )
                context = self._accuracy_review_context(
                    schedule=current,
                    snapshot=snapshot,
                    checkpoint_from=checkpoint_from,
                    checkpoint_through=checkpoint_through,
                )
                current["pending_review"] = {
                    "review_id": review_id,
                    "context": context,
                    "checkpoint_from": checkpoint_from,
                    "checkpoint_through": checkpoint_through,
                    "phase": "pending",
                    "claim_owner": "",
                    "claim_pid": 0,
                    "claim_until": 0.0,
                    "created_at": now,
                }
                current["refresh_attempts"] = 0
                current["next_refresh_attempt_at"] = 0.0
                current["updated_at"] = now
                self._persist(active=True)
                return True

    def claim_accuracy_review(
        self,
        *,
        owner: str,
        now: float,
    ) -> tuple[str, str] | None:
        with self._lock:
            schedule = self.accuracy_schedule
            pending = (
                schedule.get("pending_review")
                if isinstance(schedule, dict)
                else None
            )
            if (
                self._closed
                or self._terminal
                or not isinstance(pending, dict)
                or not pending.get("review_id")
                or pending.get("phase") == "dispatched"
            ):
                return None
            claim_owner = str(pending.get("claim_owner") or "")
            if (
                claim_owner
                and float(pending.get("claim_until") or 0) > now
                and _pid_alive(pending.get("claim_pid"))
            ):
                return None
            pending["phase"] = "claimed"
            pending["claim_owner"] = str(owner)
            pending["claim_pid"] = os.getpid()
            pending["claim_until"] = (
                now + ACCURACY_REVIEW_CLAIM_SECONDS
            )
            schedule["updated_at"] = now
            self._persist(active=True)
            return (
                str(pending["review_id"]),
                str(pending.get("context") or ""),
            )

    def mark_accuracy_review_dispatched(self, review_id: str) -> bool:
        with self._lock:
            pending = (
                self.accuracy_schedule.get("pending_review")
                if isinstance(self.accuracy_schedule, dict)
                else None
            )
            if (
                not isinstance(pending, dict)
                or pending.get("review_id") != str(review_id)
            ):
                return False
            pending["phase"] = "dispatched"
            pending["claim_until"] = 0.0
            self.accuracy_schedule["updated_at"] = time.time()
            self._persist(active=True)
            return True

    def complete_accuracy_review(self, review_id: str) -> bool:
        with self._lock:
            schedule = self.accuracy_schedule
            pending = (
                schedule.get("pending_review")
                if isinstance(schedule, dict)
                else None
            )
            if (
                not isinstance(pending, dict)
                or pending.get("review_id") != str(review_id)
            ):
                return False
            schedule["next_checkpoint"] = max(
                int(schedule.get("next_checkpoint") or 1),
                int(pending.get("checkpoint_through") or 0) + 1,
            )
            schedule["pending_review"] = {}
            schedule["updated_at"] = time.time()
            self._persist(active=True)
            return True

    def accuracy_review_is_current(self, review_id: str) -> bool:
        with self._lock:
            pending = (
                self.accuracy_schedule.get("pending_review")
                if isinstance(self.accuracy_schedule, dict)
                else None
            )
            return bool(
                not self._closed
                and not self._terminal
                and isinstance(pending, dict)
                and pending.get("review_id") == str(review_id)
            )

    def continuing_round(self) -> str:
        with self._lock:
            if self.pending_reply or self._deferred or self._terminal:
                return self.task_id
        self.observe("continuing")
        return self.ensure("continuing")

    def request_running(self) -> None:
        with self._lock:
            if self._defer_pause_reason == "blocker":
                self._timer_dirty = True
            else:
                self._deferred = False
                self._desired_timer_state = "running"
                self._desired_pause_reason = ""
                self._timer_dirty = bool(self.task_id)
            self._next_timer_attempt_at = 0.0
            self._persist(active=True)

    def ensure(self, reason: str = "working") -> str:
        """Replay only an exact manager-authored task/create intent."""
        with self._io_lock:
            with self._lock:
                if self._closed or self._terminal or not self._renew_lease_locked():
                    return self.task_id if self.task_confirmed else ""
                if reason:
                    self.observe(reason)
                if self.task_id and self.task_confirmed:
                    return self.task_id
                now = time.time()
                if now < self._next_create_attempt_at:
                    return ""
                if self._model_create_started_at or self._runtime_create_inflight:
                    return ""
                spec = deepcopy(self._pending_create_spec)
                if not spec:
                    return ""
                intended_task_id = str(
                    (spec.get("data") or {}).get("task_id") or self.task_id
                )
                if not intended_task_id:
                    return ""
                self.task_id = intended_task_id
                self._runtime_create_inflight = True
                self._persist(active=True)

            result = execute_work_update(spec, self.contact_id)
            snapshot = (
                {}
                if _successful(result)
                else refresh_task_snapshot(self.contact_id, intended_task_id)
            )
            with self._lock:
                self._runtime_create_inflight = False
                accepted = _successful(result) or (
                    str(snapshot.get("task_id") or "") == intended_task_id
                )
                if accepted:
                    self.task_confirmed = True
                    self._pending_create_spec = {}
                    self._create_attempts = 0
                    self._next_create_attempt_at = 0.0
                    self._last_durable_heartbeat_at = time.time()
                    self._last_durable_description = str(
                        snapshot.get("description") or self.base_description
                    )
                    self._timer_dirty = True
                    self._schedule_accuracy_from_data_locked(
                        intended_task_id,
                        spec.get("data"),
                    )
                    self._persist(active=True)
                    return self.task_id
                self._create_attempts += 1
                self._next_create_attempt_at = _retry_at(self._create_attempts)
                self._persist(active=True)
                return ""

    def prepare_work_update(
        self,
        tool_spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        with self._io_lock:
            return self._prepare_work_update_locked(tool_spec)

    def _prepare_work_update_locked(
        self,
        tool_spec: dict[str, Any],
    ) -> list[dict[str, Any]]:
        original = deepcopy(tool_spec)
        action = str(original.get("action") or original.get("type") or "").lower()
        if action not in {"task/create"} | _TERMINAL_ACTIONS and not self.task_id:
            return [original]

        with self._lock:
            requested_task_id = str(
                original.get("task_id")
                or (original.get("data") or {}).get("task_id")
                or ""
            )
            mapped_task_id = self.task_aliases.get(requested_task_id, "")
            if mapped_task_id:
                original["task_id"] = mapped_task_id
                if isinstance(original.get("data"), dict):
                    original["data"].pop("task_id", None)
            elif not requested_task_id and self.task_id:
                original["task_id"] = self.task_id

            requested_todo_id = str(
                original.get("todo_id")
                or (original.get("data") or {}).get("todo_id")
                or ""
            )
            if requested_todo_id:
                original["todo_id"] = self.todo_aliases.get(
                    requested_todo_id, requested_todo_id
                )
                if isinstance(original.get("data"), dict):
                    original["data"].pop("todo_id", None)

            if action != "task/create":
                return [original]

            data = deepcopy(original.get("data") or {})
            todos = data.get("todos")
            if not self.task_id:
                self.task_id = str(
                    data.get("task_id")
                    or _stable_id("task-model", self.contact_id, self.run_id)
                )
                data["task_id"] = self.task_id
                data.setdefault(
                    "client_id",
                    _stable_id("create-model-task", self.contact_id, self.run_id),
                )
                if isinstance(todos, list) and todos:
                    first = todos[0]
                    if isinstance(first, dict):
                        self.todo_id = str(
                            first.get("todo_id")
                            or _stable_id("todo-model", self.task_id, 0)
                        )
                        first["todo_id"] = self.todo_id
                prepared = {
                    "tool": "work_update",
                    "action": "task/create",
                    "data": data,
                }
                self._pending_create_spec = deepcopy(prepared)
                self._model_create_started_at = time.time()
                self._persist(active=True)
                return [prepared]

            if requested_task_id:
                self.task_aliases[requested_task_id] = self.task_id
                self.task_aliases = _bounded_mapping(self.task_aliases)
            todos = data.pop("todos", [])
            if not self.task_confirmed:
                data["task_id"] = self.task_id
                data["client_id"] = _stable_id(
                    "create-model-task", self.contact_id, self.run_id
                )
                if isinstance(todos, list) and todos and isinstance(
                    todos[0], dict
                ):
                    first_requested_id = str(todos[0].get("todo_id") or "")
                    if not self.todo_id:
                        self.todo_id = (
                            first_requested_id
                            or _stable_id("todo-model", self.task_id, 0)
                        )
                    if first_requested_id and self.todo_id:
                        self.todo_aliases[first_requested_id] = self.todo_id
                        self.todo_aliases = _bounded_mapping(self.todo_aliases)
                    todos[0]["todo_id"] = self.todo_id
                if isinstance(todos, list):
                    data["todos"] = todos
                prepared = {
                    "tool": "work_update",
                    "action": "task/create",
                    "data": data,
                }
                self._pending_create_spec = deepcopy(prepared)
                self._model_create_started_at = time.time()
                self._persist(active=True)
                return [prepared]

            for key in ("task_id", "client_id", "room_id", "schema_version"):
                data.pop(key, None)
            rewritten = [
                {
                    "tool": "work_update",
                    "action": "task/update",
                    "task_id": self.task_id,
                    "data": data,
                }
            ]
            if isinstance(todos, list) and todos:
                first = deepcopy(todos[0]) if isinstance(todos[0], dict) else {}
                first_id = str(first.pop("todo_id", "") or "")
                if self.todo_id:
                    first.pop("client_id", None)
                    if first_id:
                        self.todo_aliases[first_id] = self.todo_id
                    rewritten.append(
                        {
                            "tool": "work_update",
                            "action": "todo/update",
                            "task_id": self.task_id,
                            "todo_id": self.todo_id,
                            "data": first,
                        }
                    )
                    remaining_todos = todos[1:]
                    start_index = 1
                else:
                    remaining_todos = todos
                    start_index = 0
                for index, todo in enumerate(
                    remaining_todos,
                    start_index,
                ):
                    if not isinstance(todo, dict):
                        continue
                    item = deepcopy(todo)
                    item.setdefault(
                        "todo_id",
                        _stable_id(
                            "todo-manager-adopted",
                            self.task_id,
                            index,
                            item.get("title"),
                        ),
                    )
                    if not self.todo_id:
                        self.todo_id = str(item["todo_id"])
                    item.setdefault(
                        "client_id",
                        _stable_id("add-manager-todo", self.task_id, item["todo_id"]),
                    )
                    rewritten.append(
                        {
                            "tool": "work_update",
                            "action": "todo/add",
                            "task_id": self.task_id,
                            "data": item,
                        }
                    )
            self.todo_aliases = _bounded_mapping(self.todo_aliases)
            self._persist(active=True)
            return rewritten

    def journal_worker_start(
        self,
        worker_id: str,
        worker_type: str,
        description: str,
        *,
        task_id: str = "",
    ) -> dict[str, str]:
        """Persist a non-publishable prepare record before launching a worker."""
        with self._lock:
            target_task_id = self.resolve_task_id(task_id) or self.task_id
            if (
                not target_task_id
                or (
                    worker_id not in self.pending_workers
                    and len(self.pending_workers) >= MAX_PENDING_WORKERS
                )
            ):
                return {}
            invocation_id = _stable_id(
                "worker-invocation",
                self.contact_id,
                self.run_id,
                worker_id,
            )
            group_id = _stable_id(
                "worker-group", self.contact_id, target_task_id
            )
            self.pending_workers[str(worker_id)] = {
                "worker_id": str(worker_id),
                "worker_type": str(worker_type or "worker"),
                "description": _compact(description, 500),
                "task_id": target_task_id,
                "group_id": group_id,
                "invocation_id": invocation_id,
                "phase": "prepared",
                "state": "yet_to_start",
                "state_description": "Preparing to launch",
                "attempts": 0,
                "next_attempt_at": 0.0,
                "prepared_at": time.time(),
                "fact_updated_at": time.time(),
            }
            self.worker_delivery_watermarks.pop(str(worker_id), None)
            self._persist(active=True)
            return {
                "task_id": target_task_id,
                "group_id": group_id,
                "invocation_id": invocation_id,
            }

    def mark_worker_started(
        self,
        worker_id: str,
        *,
        queued: bool,
    ) -> dict[str, str]:
        with self._lock:
            intent = self.pending_workers.get(str(worker_id))
            if not isinstance(intent, dict):
                return {}
            intent["phase"] = "launched"
            intent["state"] = "yet_to_start" if queued else "in_progress"
            intent["state_description"] = (
                "Queued and waiting to launch"
                if queued
                else f"{str(intent.get('worker_type') or 'worker').capitalize()} "
                "worker is running"
            )
            intent["next_attempt_at"] = 0.0
            intent["fact_updated_at"] = time.time()
            self._persist(active=True)
        delivered = self._deliver_pending_workers(force=True)
        if str(worker_id) not in delivered:
            delivered.update(self._deliver_pending_workers(force=True))
        return delivered.get(str(worker_id), {})

    def discard_worker_intent(self, worker_id: str) -> None:
        with self._lock:
            worker_id = str(worker_id)
            removed = self.pending_workers.pop(worker_id, None)
            if removed is not None:
                self.worker_delivery_watermarks[worker_id] = max(
                    time.time(),
                    float(
                        removed.get("fact_updated_at") or 0
                        if isinstance(removed, dict)
                        else 0
                    ),
                )
                self._persist(active=True)
        self.close_if_terminal()

    def record_pending_worker_state(
        self,
        worker_id: str,
        state_name: str,
        description: str = "",
    ) -> bool:
        with self._lock:
            intent = self.pending_workers.get(str(worker_id))
            if not isinstance(intent, dict):
                return False
            intent["phase"] = "launched"
            intent["state"] = str(state_name)
            if description:
                intent["state_description"] = _compact(description, 500)
            intent["next_attempt_at"] = 0.0
            intent["fact_updated_at"] = time.time()
            self._persist(active=True)
            return True

    def record_work_update(
        self,
        original: dict[str, Any],
        prepared: list[dict[str, Any]],
        results: list[Any],
    ) -> None:
        action = str(original.get("action") or original.get("type") or "").lower()
        successful = bool(results) and all(_successful(item) for item in results)
        requested_task_id = str(
            original.get("task_id")
            or (original.get("data") or {}).get("task_id")
            or ""
        )
        with self._lock:
            targets_current = (
                not requested_task_id
                or self.resolve_task_id(requested_task_id) == self.task_id
                or action == "task/create"
            )
            if action == "task/create":
                self._model_create_started_at = 0.0
                if successful:
                    accepted_id = str(
                        self.task_id
                        or active_task_id(self.contact_id)
                        or requested_task_id
                    )
                    if accepted_id:
                        self.task_id = accepted_id
                        self.task_confirmed = True
                        self._pending_create_spec = {}
                        self._create_attempts = 0
                        self._next_create_attempt_at = 0.0
                else:
                    self._create_attempts += 1
                    self._next_create_attempt_at = _retry_at(
                        self._create_attempts
                    )
                data = original.get("data")
                if successful and isinstance(data, dict):
                    requested = str(data.get("task_id") or "")
                    if requested and self.task_id:
                        self.task_aliases[requested] = self.task_id
                    if data.get("title"):
                        self.title = _compact(data["title"], 120)
                    if data.get("description"):
                        self.base_description = _compact(
                            data["description"], 1_500
                        )
                    self._schedule_accuracy_from_data_locked(
                        self.task_id,
                        data,
                    )
            elif action == "task/update" and successful and targets_current:
                data = original.get("data")
                if isinstance(data, dict):
                    if data.get("description"):
                        self.base_description = _compact(
                            data["description"], 1_500
                        )
                    timer_state = str(data.get("timer_state") or "")
                    if timer_state in {"running", "paused"}:
                        self._desired_timer_state = timer_state
                        self._desired_pause_reason = str(
                            data.get("timer_pause_reason") or ""
                        )
                        self._timer_dirty = True
                    self._schedule_accuracy_from_data_locked(
                        self.task_id,
                        data,
                    )
            elif action == "blocker/create" and successful and targets_current:
                self._deferred = True
                self._defer_pause_reason = "blocker"
                self._desired_timer_state = "paused"
                self._desired_pause_reason = "blocker"
                self._timer_dirty = True
                self.latest_activity = "Waiting for a blocker to be resolved"
            elif action == "blocker/resolve" and successful and targets_current:
                self._blocker_resolution_pending = True
                self._timer_dirty = True
                self._next_timer_attempt_at = 0.0
            elif action in _TERMINAL_ACTIONS and successful and targets_current:
                self._terminal = True
                self._settle_requested = False
                self._desired_timer_state = "stopped"
                self._timer_dirty = False
                self._cancel_accuracy_schedule_locked()
            self.task_aliases = _bounded_mapping(self.task_aliases)
            self._persist(active=True)

    def queue_final_reply(self, message: str) -> str:
        """Persist prose and its stable id before any terminal network write."""
        text = str(message or "")
        if len(text) > MAX_PENDING_REPLY_CHARS:
            text = text[:MAX_PENDING_REPLY_CHARS]
        with self._lock:
            if self.pending_reply:
                return str(self.pending_reply.get("client_id") or "")
            client_id = _stable_id(
                "final-reply", self.contact_id, self.run_id, text
            )
            self.pending_reply = {
                "message": text,
                "client_id": client_id,
                "attempts": 0,
                "next_attempt_at": 0.0,
                "created_at": time.time(),
            }
            self._settle_requested = bool(self.task_id)
            self._persist(active=True)
            return client_id

    def deliver_final_reply(
        self,
        message: str,
        *,
        has_active_workers: bool,
        reply_sender: Callable[..., str] | None = None,
    ) -> str:
        self.queue_final_reply(message)
        return self._flush_final_reply(
            has_active_workers=has_active_workers,
            reply_sender=reply_sender,
            force=True,
        )

    def _flush_final_reply(
        self,
        *,
        has_active_workers: bool | None = None,
        reply_sender: Callable[..., str] | None = None,
        force: bool = False,
    ) -> str:
        with self._reply_lock:
            return self._flush_final_reply_locked(
                has_active_workers=has_active_workers,
                reply_sender=reply_sender,
                force=force,
            )

    def _flush_final_reply_locked(
        self,
        *,
        has_active_workers: bool | None,
        reply_sender: Callable[..., str] | None,
        force: bool,
    ) -> str:
        with self._lock:
            pending = deepcopy(self.pending_reply)
            if not pending:
                return "Message sent" if self._final_reply_sent else "No reply queued"
            now = time.time()
            if not force and now < float(pending.get("next_attempt_at") or 0):
                return "Message queued for durable delivery"

        self._reconcile_worker_intents()
        self._deliver_pending_workers(force=force)
        if has_active_workers is None:
            has_active_workers = self._has_active_workers_now()

        with self._lock:
            task_required = bool(
                self.task_id
                or self._pending_create_spec
                or self._create_attempts
            )
            cannot_settle = (
                bool(has_active_workers)
                or bool(self.pending_workers)
                or self._deferred
            )
            if task_required and (
                cannot_settle or not self.task_confirmed
            ):
                self._settle_requested = True
                self._persist(active=True)
                return "Message queued behind the durable work update"

        if task_required and not self._terminal:
            if not self._settle_task():
                return "Message queued behind the durable work update"

        sender = reply_sender or self.reply_sender
        if sender is None:
            return "Message queued for durable delivery"
        with self._lock:
            pending = deepcopy(self.pending_reply)
            if not pending:
                return "Message sent"
        try:
            status = sender(
                str(pending.get("message") or ""),
                self.contact_id,
                work_continues=False,
                client_id=str(pending.get("client_id") or ""),
            )
        except Exception:
            status = "Message delivery failed"
        with self._lock:
            current = self.pending_reply
            if (
                current
                and current.get("client_id") == pending.get("client_id")
                and status == "Message sent"
            ):
                self.pending_reply = {}
                self._final_reply_sent = True
                self._close_locked()
                should_unregister = True
            else:
                attempts = int(current.get("attempts") or 0) + 1
                terminal = (
                    _terminal_reply_delivery_status(status)
                    or attempts >= MAX_PENDING_REPLY_ATTEMPTS
                )
                if terminal:
                    self.pending_reply = {}
                    self._close_locked()
                    should_unregister = True
                else:
                    current["attempts"] = attempts
                    current["next_attempt_at"] = _retry_at(attempts)
                    self._persist(active=True)
                    should_unregister = False
        if should_unregister:
            _unregister(self)
        if status == "Message sent":
            return status
        if terminal:
            print(
                "[Long task] final reply delivery abandoned after a "
                "non-retryable or exhausted failure",
                flush=True,
            )
            return "Message delivery abandoned"
        return "Message queued for durable delivery"

    def terminalize_before_reply(self, *, has_active_workers: bool) -> bool:
        """Compatibility helper: settle the card, but never bypass the barrier."""
        self._reconcile_worker_intents()
        self._deliver_pending_workers(force=True)
        with self._lock:
            if (
                has_active_workers
                or self.pending_workers
                or self._deferred
                or not self.task_id
                or not self.task_confirmed
            ):
                self._settle_requested = bool(self.task_id)
                self._persist(active=True)
                return False
            if self._terminal:
                return True
            self._settle_requested = True
            self._persist(active=True)
        return self._settle_task(close_terminal=False)

    def record_reply(self, *, work_continues: bool, successful: bool) -> None:
        # Retained for older call sites. Final delivery is owned by
        # deliver_final_reply and therefore cannot be marked here.
        if work_continues:
            return
        with self._lock:
            if successful and not self.pending_reply:
                self._final_reply_sent = True

    def defer(
        self,
        note: str = "",
        *,
        pause_reason: str = "infrastructure",
    ) -> None:
        with self._lock:
            reason = (
                pause_reason
                if pause_reason
                in {"rate_limited", "offline", "infrastructure", "blocker"}
                else "infrastructure"
            )
            self._deferred = True
            self._defer_pause_reason = reason
            self._desired_timer_state = "paused"
            self._desired_pause_reason = reason
            self._timer_dirty = bool(self.task_id)
            self._next_timer_attempt_at = 0.0
            if note:
                self.latest_activity = _compact(note, 200)
            self._persist(active=True)

    def finish(self, *, keep_alive: bool = False) -> None:
        """Stop manager heartbeats; recovery owns every outstanding intent."""
        with self._lock:
            self._manager_running = False
            if self._closed:
                return
            if keep_alive and not self._terminal:
                self.latest_activity = "Workers are processing the request"
            # Never infer successful completion merely because a manager turn
            # ended.  A final reply or explicit terminal action is the fence.
            if (
                not self.task_id
                and not self.pending_reply
                and not self.pending_workers
                and not self._pending_create_spec
            ):
                self._close_locked()
                should_unregister = True
            else:
                self._persist(active=True)
                should_unregister = False
        if should_unregister:
            _unregister(self)

    def replay_pending_once(self, *, recovery: bool = False) -> None:
        """Replay every durable phase without requiring a new inbound message."""
        if not self.is_open:
            return
        self._reconcile_worker_intents(force_prepared=recovery)
        with self._lock:
            create_due = bool(
                self._pending_create_spec
                and not self.task_confirmed
            )
        if create_due:
            self.ensure("")
        self._deliver_pending_workers(force=True)
        self._reconcile_timer(force=True)
        with self._lock:
            settle_due = self._settle_requested and not self.pending_reply
            reply_due = bool(self.pending_reply)
        if settle_due:
            self._settle_task()
        if reply_due:
            self._flush_final_reply(force=True)
        self._prepare_accuracy_review_if_due()
        self.close_if_terminal()

    def _watch(self) -> None:
        self.replay_pending_once(recovery=self._recovery_mode)
        self._recovery_mode = False
        while not self._stop.wait(1.0):
            self._reconcile_worker_intents()
            now = time.time()
            with self._lock:
                if self._closed or not self._renew_lease_locked():
                    return
                manager_running = self._manager_running
                activity_due = (
                    manager_running
                    and now - self._last_activity_heartbeat_at
                    >= self.activity_heartbeat_seconds
                )
                create_due = (
                    bool(self._pending_create_spec)
                    and not self.task_confirmed
                    and now >= self._next_create_attempt_at
                    and not self._model_create_started_at
                )
                durable_due = (
                    bool(self.task_id and self.task_confirmed)
                    and now - self._last_durable_heartbeat_at
                    >= self.durable_heartbeat_seconds
                    and now >= self._next_heartbeat_attempt_at
                )
                timer_due = (
                    self._timer_dirty
                    and self.task_confirmed
                    and now >= self._next_timer_attempt_at
                )
                settle_due = (
                    self._settle_requested
                    and self.task_confirmed
                    and not self.pending_reply
                    and now >= self._next_settle_attempt_at
                )
                worker_due = any(
                    isinstance(item, dict)
                    and item.get("phase") in {"launched", "published"}
                    and now >= float(item.get("next_attempt_at") or 0)
                    for item in self.pending_workers.values()
                )
                reply_due = bool(self.pending_reply) and now >= float(
                    self.pending_reply.get("next_attempt_at") or 0
                )
                activity = self.latest_activity
                if activity_due:
                    self._last_activity_heartbeat_at = now
            if activity_due and self.activity_heartbeat is not None:
                try:
                    self.activity_heartbeat(activity)
                except Exception:
                    pass
            if create_due:
                self.ensure("")
            if worker_due:
                self._deliver_pending_workers()
            if timer_due:
                self._reconcile_timer()
            if settle_due:
                self._settle_task()
            if reply_due:
                self._flush_final_reply()
            elif durable_due:
                self._heartbeat(activity)
            self._prepare_accuracy_review_if_due()
            if self.close_if_terminal():
                return

    def _reconcile_worker_intents(
        self,
        *,
        force_prepared: bool = False,
    ) -> None:
        resolver = self.worker_status_resolver
        if resolver is None:
            return
        now = time.time()
        with self._lock:
            prepared = [
                str(worker_id)
                for worker_id, intent in self.pending_workers.items()
                if isinstance(intent, dict)
                and intent.get("phase") == "prepared"
                and (
                    force_prepared
                    or now - float(intent.get("prepared_at") or now)
                    >= PREPARED_RECONCILE_GRACE_SECONDS
                )
            ]
        for worker_id in prepared:
            try:
                status = str(resolver(worker_id, self.contact_id) or "")
            except Exception:
                continue
            lowered = status.lower()
            if "not found" in lowered or "does not belong" in lowered:
                self.discard_worker_intent(worker_id)
                continue
            if "queued" in lowered:
                state, description = "yet_to_start", "Queued and waiting to launch"
            elif "running" in lowered or "is active" in lowered:
                state, description = "in_progress", "Worker is running"
            elif (
                "completed" in lowered
                or "is idle" in lowered
                or "archived run" in lowered
            ):
                state, description = "completed", "Worker completed"
            else:
                continue
            with self._lock:
                intent = self.pending_workers.get(worker_id)
                if not isinstance(intent, dict) or intent.get("phase") != "prepared":
                    continue
                intent["phase"] = "launched"
                intent["state"] = state
                intent["state_description"] = description
                intent["fact_updated_at"] = time.time()
                intent["next_attempt_at"] = 0.0
                self._persist(active=True)

    def _deliver_pending_workers(
        self,
        *,
        force: bool = False,
    ) -> dict[str, dict[str, str]]:
        delivered: dict[str, dict[str, str]] = {}
        try:
            with self._io_lock:
                with self._lock:
                    if not self.task_confirmed or self._terminal:
                        return delivered
                    self._merge_external_worker_facts_locked()
                    now = time.time()
                    intents = [
                        deepcopy(intent)
                        for intent in self.pending_workers.values()
                        if isinstance(intent, dict)
                        and intent.get("phase") in {"launched", "published"}
                        and (
                            force
                            or now
                            >= float(intent.get("next_attempt_at") or 0)
                        )
                    ]
                for intent in intents:
                    worker_id = str(intent.get("worker_id") or "")
                    if not worker_id:
                        continue
                    target_task_id = str(intent.get("task_id") or "")
                    with self._lock:
                        target_task_id = self.task_aliases.get(
                            target_task_id, target_task_id
                        )
                    if intent.get("phase") == "published":
                        state_delivered = record_worker_state(
                            self.contact_id,
                            worker_id,
                            str(intent.get("state") or "in_progress"),
                            str(intent.get("state_description") or ""),
                        )
                        reference = (
                            deepcopy(
                                intent.get("published_reference")
                                or {
                                    "task_id": target_task_id,
                                    "group_id": str(
                                        intent.get("group_id") or ""
                                    ),
                                    "invocation_id": str(
                                        intent.get("invocation_id") or ""
                                    ),
                                }
                            )
                            if state_delivered
                            else {}
                        )
                    else:
                        reference = record_worker_started(
                            self.contact_id,
                            worker_id,
                            str(intent.get("worker_type") or "worker"),
                            str(intent.get("description") or worker_id),
                            queued=intent.get("state") == "yet_to_start",
                            task_id=target_task_id,
                            invocation_id=str(
                                intent.get("invocation_id") or ""
                            ),
                            state_name=str(
                                intent.get("state") or "in_progress"
                            ),
                            state_description=str(
                                intent.get("state_description") or ""
                            ),
                        )
                    with self._lock:
                        self._merge_external_worker_facts_locked()
                        current = self.pending_workers.get(worker_id)
                        if (
                            not isinstance(current, dict)
                            or current.get("invocation_id")
                            != intent.get("invocation_id")
                        ):
                            continue
                        if reference:
                            if intent.get("phase") == "published":
                                current_updated = float(
                                    current.get("fact_updated_at") or 0
                                )
                                sent_updated = float(
                                    intent.get("fact_updated_at") or 0
                                )
                                if current_updated > sent_updated:
                                    current["phase"] = "published"
                                    current["published_reference"] = deepcopy(
                                        reference
                                    )
                                    current["attempts"] = 0
                                    current["next_attempt_at"] = 0.0
                                else:
                                    self.pending_workers.pop(worker_id, None)
                                    self.worker_delivery_watermarks[
                                        worker_id
                                    ] = max(time.time(), current_updated)
                                    delivered[worker_id] = reference
                            else:
                                current_updated = float(
                                    current.get("fact_updated_at") or 0
                                )
                                sent_updated = float(
                                    intent.get("fact_updated_at") or 0
                                )
                                if current_updated > sent_updated:
                                    # A real worker fact raced the initial
                                    # create. Keep the accepted correlation
                                    # until that newer fact is delivered.
                                    current["phase"] = "published"
                                    current["published_reference"] = deepcopy(
                                        reference
                                    )
                                    current["attempts"] = 0
                                    current["next_attempt_at"] = 0.0
                                else:
                                    # The accepted create already carried the
                                    # latest known state, so there is no
                                    # second mutation to replay.
                                    self.pending_workers.pop(worker_id, None)
                                    self.worker_delivery_watermarks[
                                        worker_id
                                    ] = max(time.time(), current_updated)
                                    delivered[worker_id] = reference
                        else:
                            attempts = int(current.get("attempts") or 0) + 1
                            current["attempts"] = attempts
                            current["next_attempt_at"] = _retry_at(attempts)
                        self._persist(active=True)
        finally:
            # Terminal observation can race the last delivery. Re-evaluate
            # only after the IO/worker locks are released.
            self.close_if_terminal()
        return delivered

    def _merge_external_worker_facts_locked(self) -> None:
        entry = _state_entry(self.contact_id)
        external_workers = entry.get("pending_workers")
        if not isinstance(external_workers, dict):
            return
        for worker_id, external in external_workers.items():
            if not isinstance(external, dict):
                continue
            local = self.pending_workers.get(str(worker_id))
            external_updated = float(external.get("fact_updated_at") or 0)
            watermark = float(
                self.worker_delivery_watermarks.get(str(worker_id)) or 0
            )
            if external_updated <= watermark:
                continue
            if not isinstance(local, dict) or external_updated > float(
                local.get("fact_updated_at") or 0
            ):
                merged = deepcopy(external)
                if not isinstance(local, dict):
                    merged["phase"] = "published"
                elif local.get("phase") == "published":
                    merged["phase"] = "published"
                    merged["published_reference"] = deepcopy(
                        local.get("published_reference") or {}
                    )
                self.pending_workers[str(worker_id)] = merged

    def _reconcile_timer(self, *, force: bool = False) -> bool:
        with self._io_lock:
            with self._lock:
                if (
                    not self.task_id
                    or not self.task_confirmed
                    or self._terminal
                    or (
                        not force
                        and time.time() < self._next_timer_attempt_at
                    )
                ):
                    return False
                task_id = self.task_id
                desired_state = self._desired_timer_state
                desired_reason = self._desired_pause_reason
                blocker_resolution_pending = self._blocker_resolution_pending
            snapshot = refresh_task_snapshot(self.contact_id, task_id)
            with self._lock:
                if task_id != self.task_id:
                    return False
                remote_state = str(snapshot.get("state") or "")
                if remote_state in _TERMINAL_STATES:
                    self._terminal = True
                    self._timer_dirty = False
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return True
                remote_timer = str(snapshot.get("timer_state") or "")
                remote_reason = str(snapshot.get("timer_pause_reason") or "")
                if blocker_resolution_pending:
                    if remote_timer == "paused" and remote_reason == "blocker":
                        self._deferred = True
                        self._defer_pause_reason = "blocker"
                        self._desired_timer_state = "paused"
                        self._desired_pause_reason = "blocker"
                    else:
                        self._deferred = False
                        self._defer_pause_reason = "infrastructure"
                        self._desired_timer_state = "running"
                        self._desired_pause_reason = ""
                    self._blocker_resolution_pending = False
                    desired_state = self._desired_timer_state
                    desired_reason = self._desired_pause_reason
                if (
                    desired_state == "running"
                    and remote_timer == "paused"
                    and remote_reason == "blocker"
                ):
                    self._deferred = True
                    self._defer_pause_reason = "blocker"
                    self._desired_timer_state = "paused"
                    self._desired_pause_reason = "blocker"
                    self._timer_dirty = False
                    self._persist(active=True)
                    return True
                matches = remote_timer == desired_state and (
                    desired_state != "paused" or remote_reason == desired_reason
                )
                if matches:
                    self._timer_dirty = False
                    self._timer_attempts = 0
                    self._next_timer_attempt_at = 0.0
                    self._persist(active=True)
                    return True
                if not snapshot:
                    self._timer_attempts += 1
                    self._next_timer_attempt_at = _retry_at(
                        self._timer_attempts
                    )
                    self._persist(active=True)
                    return False
                data: dict[str, Any] = {"timer_state": desired_state}
                data["timer_pause_reason"] = (
                    desired_reason if desired_state == "paused" else None
                )
            result = execute_work_update(
                {
                    "tool": "work_update",
                    "action": "task/update",
                    "task_id": task_id,
                    "data": data,
                },
                self.contact_id,
            )
            with self._lock:
                if _successful(result):
                    self._timer_dirty = False
                    self._timer_attempts = 0
                    self._next_timer_attempt_at = 0.0
                    self._persist(active=True)
                    return True
                # Keep desired state durable. A later refresh proves a lost
                # response without issuing conflicting timer transitions.
                self._timer_attempts += 1
                self._next_timer_attempt_at = _retry_at(self._timer_attempts)
                self._persist(active=True)
                return False

    def _heartbeat(self, activity: str) -> bool:
        with self._io_lock:
            with self._lock:
                if not self.task_id or not self.task_confirmed or self._terminal:
                    return False
                task_id = self.task_id
                description = self._activity_description(activity)
                if description == self._last_durable_description:
                    self._last_durable_heartbeat_at = time.time()
                    return True
            snapshot = refresh_task_snapshot(self.contact_id, task_id)
            with self._lock:
                if task_id != self.task_id or self._terminal:
                    return False
                if snapshot.get("state") in _TERMINAL_STATES:
                    self._terminal = True
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return True
                remote_description = str(snapshot.get("description") or "")
                if (
                    remote_description
                    and remote_description != self._last_durable_description
                    and remote_description != description
                ):
                    marker = "\n\nLatest activity:"
                    self.base_description = (
                        remote_description.rsplit(marker, 1)[0]
                        if marker in remote_description
                        else remote_description
                    )
                    description = self._activity_description(activity)
                if remote_description == description:
                    self._record_heartbeat_success_locked(description)
                    return True
                spec = {
                    "tool": "work_update",
                    "action": "task/update",
                    "task_id": task_id,
                    "data": {"description": description},
                }
            result = execute_work_update(spec, self.contact_id)
            with self._lock:
                if _successful(result):
                    self._record_heartbeat_success_locked(description)
                    return True
                self._heartbeat_attempts += 1
                self._next_heartbeat_attempt_at = _retry_at(
                    self._heartbeat_attempts
                )
                self._persist(active=True)
                return False

    def _record_heartbeat_success_locked(self, description: str) -> None:
        self._last_durable_description = description
        self._last_durable_heartbeat_at = time.time()
        self._heartbeat_attempts = 0
        self._next_heartbeat_attempt_at = 0.0
        self._persist(active=True)

    def _activity_description(self, activity: str) -> str:
        description = self.base_description.strip()
        if description:
            description += "\n\n"
        return description + f"Latest activity: {_compact(activity, 200)}."

    def _settle_task(self, *, close_terminal: bool = True) -> bool:
        try:
            return self._settle_task_inner()
        finally:
            # A remote terminal snapshot clears the durable settle guard.
            # Re-run cleanup after the IO lock is gone so queued roots can
            # advance even when this method is invoked outside the watcher.
            if close_terminal:
                self.close_if_terminal()

    def _settle_task_inner(self) -> bool:
        with self._io_lock:
            with self._lock:
                now = time.time()
                if (
                    not self.task_id
                    or not self.task_confirmed
                    or now < self._next_settle_attempt_at
                ):
                    return False
                task_id = self.task_id
            snapshot = refresh_task_snapshot(self.contact_id, task_id)
            with self._lock:
                task_state = str(snapshot.get("state") or "")
                if task_state in _TERMINAL_STATES:
                    self._terminal = True
                    self._settle_requested = False
                    self._cancel_accuracy_schedule_locked()
                    self._persist(active=True)
                    return True
                if task_state == "blocked" or (
                    snapshot.get("timer_state") == "paused"
                    and snapshot.get("timer_pause_reason") == "blocker"
                ):
                    self._deferred = True
                    self._defer_pause_reason = "blocker"
                    self._desired_timer_state = "paused"
                    self._desired_pause_reason = "blocker"
                    self._timer_dirty = False
                    self.latest_activity = "Waiting for a blocker to be resolved"
                    self._settle_requested = True
                    self._persist(active=True)
                    return False

            terminal_spec = {
                "tool": "work_update",
                "action": "task/complete",
                "task_id": task_id,
                "data": {
                    "work_event_id": _stable_id(
                        "complete-manager-task", task_id
                    ),
                    "body": "Request completed.",
                    "client_id": _stable_id(
                        "complete-manager-task-client", task_id
                    ),
                },
            }
            terminal_result = execute_work_update(
                terminal_spec, self.contact_id
            )
            accepted = _successful(terminal_result)
            if not accepted:
                proof = refresh_task_snapshot(self.contact_id, task_id)
                accepted = str(proof.get("state") or "") in _TERMINAL_STATES
            with self._lock:
                if not accepted:
                    self._schedule_settle_retry_locked(now)
                    return False
                self._terminal = True
                self._settle_requested = False
                self._settle_attempts = 0
                self._next_settle_attempt_at = 0.0
                self._desired_timer_state = "stopped"
                self._timer_dirty = False
                self._cancel_accuracy_schedule_locked()
                self._persist(active=True)
                return True

    def _schedule_settle_retry_locked(self, _now: float) -> None:
        self._settle_requested = True
        self._settle_attempts += 1
        self._next_settle_attempt_at = _retry_at(self._settle_attempts)
        self._persist(active=True)

    def _has_active_workers_now(self) -> bool:
        callback = self.has_active_workers
        if callback is None:
            return bool(self.pending_workers)
        try:
            return bool(callback(self.contact_id))
        except Exception:
            return True

    def _renew_lease_locked(self) -> bool:
        claimed = _claim_contact(
            self.contact_id,
            self._lease_owner,
            expected_run_id=self.run_id,
            allow_create=False,
        )
        return claimed is not None

    def _close_locked(self) -> None:
        self._closed = True
        self._stop.set()
        entry = self._state_payload(active=False)
        now = time.time()

        def mutate(state: dict[str, Any]) -> None:
            contacts = state.setdefault("contacts", {})
            current = contacts.get(self.contact_id)
            if (
                isinstance(current, dict)
                and current.get("lease_owner") == self._lease_owner
            ):
                contacts[self.contact_id] = _tombstone(entry, now)
            _prune_state_locked(state, now)

        update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)

    def _state_payload(self, *, active: bool) -> dict[str, Any]:
        return {
            "active": bool(active),
            "contact_id": self.contact_id,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "task_id": self.task_id,
            "task_confirmed": self.task_confirmed,
            "todo_id": self.todo_id,
            "title": _compact(self.title, 120),
            "base_description": _compact(self.base_description, 1_500),
            "latest_activity": _compact(self.latest_activity, 200),
            "task_aliases": _bounded_mapping(self.task_aliases),
            "todo_aliases": _bounded_mapping(self.todo_aliases),
            "pending_workers": deepcopy(
                dict(list(self.pending_workers.items())[-MAX_PENDING_WORKERS:])
            ),
            "worker_delivery_watermarks": deepcopy(
                dict(
                    list(self.worker_delivery_watermarks.items())[
                        -MAX_PENDING_WORKERS:
                    ]
                )
            ),
            "pending_reply": deepcopy(self.pending_reply),
            "accuracy_schedule": deepcopy(self.accuracy_schedule),
            "pending_create_spec": deepcopy(self._pending_create_spec),
            "create_attempts": self._create_attempts,
            "next_create_attempt_at": self._next_create_attempt_at,
            "settle_attempts": self._settle_attempts,
            "next_settle_attempt_at": self._next_settle_attempt_at,
            "settle_requested": self._settle_requested,
            "deferred": self._deferred,
            "defer_pause_reason": self._defer_pause_reason,
            "terminal": self._terminal,
            "manager_running": self._manager_running,
            "desired_timer_state": self._desired_timer_state,
            "desired_pause_reason": self._desired_pause_reason,
            "timer_dirty": self._timer_dirty,
            "timer_attempts": self._timer_attempts,
            "next_timer_attempt_at": self._next_timer_attempt_at,
            "blocker_resolution_pending": self._blocker_resolution_pending,
            "last_durable_description": self._last_durable_description,
            "heartbeat_attempts": self._heartbeat_attempts,
            "next_heartbeat_attempt_at": self._next_heartbeat_attempt_at,
            "lease_owner": self._lease_owner,
            "lease_pid": os.getpid(),
            "lease_until": time.time() + LEASE_SECONDS,
            "updated_at": time.time(),
        }

    def _persist(self, *, active: bool) -> bool:
        payload = self._state_payload(active=active)
        written = False

        def mutate(state: dict[str, Any]) -> None:
            nonlocal written
            now = time.time()
            _prune_state_locked(state, now)
            contacts = state.setdefault("contacts", {})
            current = contacts.get(self.contact_id)
            if isinstance(current, dict) and current.get("active"):
                owner = str(current.get("lease_owner") or "")
                lease_until = float(current.get("lease_until") or 0)
                if (
                    owner
                    and owner != self._lease_owner
                    and lease_until > now
                    and _pid_alive(current.get("lease_pid"))
                ):
                    return
                # A worker process can publish a newer runtime fact directly
                # into the journal. Do not overwrite it with a stale copy.
                current_workers = current.get("pending_workers")
                if isinstance(current_workers, dict):
                    for worker_id, external in current_workers.items():
                        local = payload["pending_workers"].get(worker_id)
                        external_updated = float(
                            external.get("fact_updated_at") or 0
                        ) if isinstance(external, dict) else 0.0
                        watermark = float(
                            payload["worker_delivery_watermarks"].get(
                                worker_id
                            )
                            or 0
                        )
                        if (
                            isinstance(external, dict)
                            and isinstance(local, dict)
                            and external_updated
                            > float(local.get("fact_updated_at") or 0)
                        ):
                            payload["pending_workers"][worker_id] = deepcopy(
                                external
                            )
                        elif (
                            isinstance(external, dict)
                            and not isinstance(local, dict)
                            and external_updated > watermark
                        ):
                            restored = deepcopy(external)
                            restored["phase"] = "published"
                            payload["pending_workers"][worker_id] = restored
            contacts[self.contact_id] = deepcopy(payload)
            written = True

        update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)
        return written


def begin_long_task_run(
    contact_id: str,
    run_id: str,
    context: str,
    *,
    visible: bool,
    activity_heartbeat: Callable[[str], None] | None = None,
    reply_sender: Callable[..., str] | None = None,
    has_active_workers: Callable[[str], bool] | None = None,
    worker_status_resolver: Callable[[str, str], str] | None = None,
) -> LongTaskLifecycle | None:
    """Start an invisible-or-visible lifecycle or attach its continuation."""
    contact_id = str(contact_id)
    with _REGISTRY_LOCK:
        current = _ACTIVE_BY_CONTACT.get(contact_id)
        if current is not None and current.is_open:
            current.reply_sender = reply_sender or current.reply_sender
            current.has_active_workers = (
                has_active_workers or current.has_active_workers
            )
            current.worker_status_resolver = (
                worker_status_resolver or current.worker_status_resolver
            )
            current.attach(run_id, context, activity_heartbeat)
            return current
        saved = _state_entry(contact_id)
        lifecycle = LongTaskLifecycle(
            contact_id,
            run_id,
            context,
            activity_heartbeat=activity_heartbeat,
            saved=saved if saved.get("active") else None,
            reply_sender=reply_sender,
            has_active_workers=has_active_workers,
            worker_status_resolver=worker_status_resolver,
        )
        if not lifecycle.is_open:
            return None
        _ACTIVE_BY_CONTACT[contact_id] = lifecycle
        return lifecycle


def recover_long_task_lifecycles(
    *,
    reply_sender: Callable[..., str] | None = None,
    has_active_workers: Callable[[str], bool] | None = None,
    worker_status_resolver: Callable[[str, str], str] | None = None,
    limit: int = MAX_RECOVERY_CONTACTS,
) -> int:
    """Claim and replay a bounded set of active journals at process boot."""
    recovered = 0
    entries = _active_entries()[: max(0, min(int(limit), MAX_RECOVERY_CONTACTS))]
    for contact_id, saved in entries:
        lifecycle: LongTaskLifecycle | None = None
        with _REGISTRY_LOCK:
            current = _ACTIVE_BY_CONTACT.get(contact_id)
            if current is not None and current.is_open:
                continue
            owner = f"{_PROCESS_TOKEN}:{uuid.uuid4().hex}"
            claimed = _claim_contact(
                contact_id,
                owner,
                expected_run_id=str(saved.get("run_id") or ""),
                allow_create=False,
            )
            if claimed is None:
                continue
            lifecycle = LongTaskLifecycle(
                contact_id,
                str(saved.get("run_id") or ""),
                "",
                saved=claimed,
                lease_owner=owner,
                recovery=True,
                auto_start=False,
                reply_sender=reply_sender,
                has_active_workers=has_active_workers,
                worker_status_resolver=worker_status_resolver,
            )
            if not lifecycle.is_open:
                continue
            _ACTIVE_BY_CONTACT[contact_id] = lifecycle
            recovered += 1
        # Claiming is synchronous and bounded; transports replay on one daemon
        # per contact. New roots observe the active journal and are durably
        # queued, so startup never waits on N sequential network timeouts.
        lifecycle.start()
    return recovered


def _persisted_active_estimated_task_snapshots(
    *,
    limit: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Read legacy active task cache entries that predate lifecycle journals."""
    try:
        from core import work_updates as work_updates_module

        with work_updates_module._state_guard():
            state = work_updates_module._read_state()
    except Exception:
        return []
    contacts = state.get("contacts")
    if not isinstance(contacts, dict):
        return []
    found: list[tuple[str, dict[str, Any]]] = []
    for contact_id, contact in sorted(contacts.items()):
        if len(found) >= max(0, min(int(limit), MAX_RECOVERY_CONTACTS)):
            break
        if not isinstance(contact, dict):
            continue
        task_id = str(contact.get("active_task_id") or "")
        tasks = contact.get("tasks")
        snapshot = tasks.get(task_id) if isinstance(tasks, dict) else None
        if not task_id or not isinstance(snapshot, dict):
            continue
        if str(snapshot.get("state") or "") in _TERMINAL_STATES:
            continue
        estimate_present, goal_seconds = _estimate_goal_from_data(snapshot)
        if not estimate_present or not goal_seconds:
            continue
        item = deepcopy(snapshot)
        item["task_id"] = task_id
        found.append((str(contact_id), item))
    return found


def backfill_active_estimated_task_lifecycles(
    *,
    reply_sender: Callable[..., str] | None = None,
    has_active_workers: Callable[[str], bool] | None = None,
    worker_status_resolver: Callable[[str, str], str] | None = None,
    limit: int = MAX_RECOVERY_CONTACTS,
) -> int:
    """Adopt cached active estimated tasks created before journaling existed."""
    backfilled = 0
    snapshots = _persisted_active_estimated_task_snapshots(limit=limit)
    for contact_id, snapshot in snapshots:
        task_id = str(snapshot.get("task_id") or "")
        _, goal_seconds = _estimate_goal_from_data(snapshot)
        lifecycle: LongTaskLifecycle | None = None
        created = False
        changed = False
        with _REGISTRY_LOCK:
            current = _ACTIVE_BY_CONTACT.get(contact_id)
            if current is not None and current.is_open:
                lifecycle = current
            else:
                lifecycle = LongTaskLifecycle(
                    contact_id,
                    _stable_id("backfill-run", contact_id, task_id),
                    "",
                    auto_start=False,
                    recovery=True,
                    reply_sender=reply_sender,
                    has_active_workers=has_active_workers,
                    worker_status_resolver=worker_status_resolver,
                )
                if not lifecycle.is_open:
                    continue
                _ACTIVE_BY_CONTACT[contact_id] = lifecycle
                created = True

            with lifecycle._lock:
                if lifecycle._terminal or (
                    lifecycle.task_id
                    and lifecycle.task_id != task_id
                ):
                    continue
                changed = (
                    created
                    or not lifecycle.task_confirmed
                    or lifecycle.task_id != task_id
                    or not lifecycle.accuracy_schedule
                )
                lifecycle.task_id = task_id
                lifecycle.task_confirmed = True
                lifecycle.title = _compact(
                    snapshot.get("title") or lifecycle.title,
                    120,
                )
                lifecycle.base_description = _compact(
                    snapshot.get("description")
                    or lifecycle.base_description,
                    1_500,
                )
                lifecycle._last_durable_description = str(
                    snapshot.get("description") or ""
                )
                todos = snapshot.get("todos")
                if not lifecycle.todo_id:
                    if isinstance(todos, dict):
                        lifecycle.todo_id = str(
                            next(iter(todos), "")
                        )
                    elif isinstance(todos, list):
                        lifecycle.todo_id = str(
                            next(
                                (
                                    item.get("todo_id")
                                    for item in todos
                                    if isinstance(item, dict)
                                    and item.get("todo_id")
                                ),
                                "",
                            )
                        )
                elapsed = _non_negative_number(
                    snapshot.get("active_elapsed_seconds")
                )
                interval = goal_seconds / ACCURACY_REVIEW_SEGMENTS
                anchor_at = time.time() - (elapsed or interval)
                changed = (
                    lifecycle._set_accuracy_goal_locked(
                        task_id,
                        goal_seconds,
                        now=anchor_at,
                    )
                    or changed
                )
                lifecycle._persist(active=True)
                if changed:
                    backfilled += 1
        lifecycle.start()
    return backfilled


def current_long_task(contact_id: str) -> LongTaskLifecycle | None:
    with _REGISTRY_LOCK:
        lifecycle = _ACTIVE_BY_CONTACT.get(str(contact_id))
        return lifecycle if lifecycle is not None and lifecycle.is_open else None


def claim_ready_accuracy_review_roots(
    *,
    limit: int = 16,
    exclude_contacts: set[str] | None = None,
) -> dict[str, str]:
    """Claim due internal reviews for durable ManagerDispatcher admission."""
    excluded = {str(item) for item in (exclude_contacts or set())}
    owner = _PROCESS_TOKEN
    now = time.time()
    with _REGISTRY_LOCK:
        lifecycles = [
            lifecycle
            for contact_id, lifecycle in _ACTIVE_BY_CONTACT.items()
            if contact_id not in excluded and lifecycle.is_open
        ]
    lifecycles.sort(
        key=lambda lifecycle: float(
            (
                lifecycle.accuracy_schedule.get("pending_review") or {}
            ).get("created_at")
            or float("inf")
        )
    )
    claimed: dict[str, str] = {}
    for lifecycle in lifecycles:
        if len(claimed) >= max(0, min(int(limit), 64)):
            break
        result = lifecycle.claim_accuracy_review(owner=owner, now=now)
        if result is None:
            continue
        review_id, context = result
        claimed[lifecycle.contact_id] = (
            f"{_ACCURACY_REVIEW_MARKER} {review_id}\n{context}"
        )
    return claimed


def acknowledge_accuracy_review_dispatched(
    contact_id: str,
    review_id: str,
) -> bool:
    """Mark a review owned by MAINTENANCE without advancing its checkpoint."""
    contact_id = str(contact_id)
    review_id = str(review_id)
    lifecycle = current_long_task(contact_id)
    if lifecycle is not None:
        return lifecycle.mark_accuracy_review_dispatched(review_id)
    updated = False

    def mutate(state: dict[str, Any]) -> None:
        nonlocal updated
        entry = (state.get("contacts") or {}).get(contact_id)
        schedule = (
            entry.get("accuracy_schedule")
            if isinstance(entry, dict)
            else None
        )
        pending = (
            schedule.get("pending_review")
            if isinstance(schedule, dict)
            else None
        )
        if (
            not isinstance(pending, dict)
            or pending.get("review_id") != review_id
        ):
            return
        pending["phase"] = "dispatched"
        pending["claim_until"] = 0.0
        schedule["updated_at"] = time.time()
        updated = True

    update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)
    return updated


def close_terminal_accuracy_lifecycle(contact_id: str) -> bool:
    """Release a terminal task observed by an internal accuracy turn."""
    contact_id = str(contact_id)
    lifecycle = current_long_task(contact_id)
    if lifecycle is not None:
        return lifecycle.close_if_terminal()
    closed = False
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        nonlocal closed
        contacts = state.get("contacts")
        entry = (
            contacts.get(contact_id)
            if isinstance(contacts, dict)
            else None
        )
        if (
            not isinstance(entry, dict)
            or not entry.get("active")
            or not entry.get("terminal")
            or entry.get("pending_reply")
            or entry.get("pending_workers")
            or entry.get("pending_create_spec")
            or entry.get("settle_requested")
        ):
            return
        contacts[contact_id] = _tombstone(entry, now)
        closed = True

    update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)
    return closed


def complete_accuracy_review_root(
    contact_id: str,
    review_id: str,
) -> bool:
    """Advance one recurring schedule only after its manager turn succeeds."""
    contact_id = str(contact_id)
    review_id = str(review_id)
    lifecycle = current_long_task(contact_id)
    if lifecycle is not None:
        completed = lifecycle.complete_accuracy_review(review_id)
        lifecycle.close_if_terminal()
        return completed
    if close_terminal_accuracy_lifecycle(contact_id):
        return False
    updated = False

    def mutate(state: dict[str, Any]) -> None:
        nonlocal updated
        entry = (state.get("contacts") or {}).get(contact_id)
        schedule = (
            entry.get("accuracy_schedule")
            if isinstance(entry, dict)
            else None
        )
        pending = (
            schedule.get("pending_review")
            if isinstance(schedule, dict)
            else None
        )
        if (
            not isinstance(pending, dict)
            or pending.get("review_id") != review_id
        ):
            return
        schedule["next_checkpoint"] = max(
            int(schedule.get("next_checkpoint") or 1),
            int(pending.get("checkpoint_through") or 0) + 1,
        )
        schedule["pending_review"] = {}
        schedule["updated_at"] = time.time()
        updated = True

    update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)
    return updated


def accuracy_review_root_is_current(
    contact_id: str,
    review_id: str,
) -> bool:
    """Return false after task terminalization cancels an admitted review."""
    contact_id = str(contact_id)
    review_id = str(review_id)
    lifecycle = current_long_task(contact_id)
    if lifecycle is not None:
        return lifecycle.accuracy_review_is_current(review_id)
    entry = _state_entry(contact_id)
    schedule = entry.get("accuracy_schedule")
    pending = (
        schedule.get("pending_review")
        if isinstance(schedule, dict)
        else None
    )
    return bool(
        entry.get("active")
        and not entry.get("terminal")
        and isinstance(pending, dict)
        and pending.get("review_id") == review_id
    )


def record_pending_worker_state(
    contact_id: str,
    worker_id: str,
    state_name: str,
    description: str = "",
) -> bool:
    """Retain actual worker truth while its initial card is replaying."""
    lifecycle = current_long_task(contact_id)
    if lifecycle is not None:
        return lifecycle.record_pending_worker_state(
            worker_id, state_name, description
        )
    updated = False
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        nonlocal updated
        entry = (state.get("contacts") or {}).get(str(contact_id))
        if not isinstance(entry, dict) or not entry.get("active"):
            return
        intent = (entry.get("pending_workers") or {}).get(str(worker_id))
        if not isinstance(intent, dict):
            return
        intent["phase"] = "launched"
        intent["state"] = str(state_name)
        if description:
            intent["state_description"] = _compact(description, 500)
        intent["next_attempt_at"] = 0.0
        intent["fact_updated_at"] = now
        entry["updated_at"] = now
        updated = True

    update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)
    return updated


def _unregister(lifecycle: LongTaskLifecycle) -> None:
    with _REGISTRY_LOCK:
        current = _ACTIVE_BY_CONTACT.get(lifecycle.contact_id)
        if current is lifecycle:
            _ACTIVE_BY_CONTACT.pop(lifecycle.contact_id, None)


def reset_long_task_registry_for_tests() -> None:
    """Stop daemon lifecycles and release their leases between unit tests."""
    with _REGISTRY_LOCK:
        lifecycles = list(_ACTIVE_BY_CONTACT.values())
        _ACTIVE_BY_CONTACT.clear()
    for lifecycle in lifecycles:
        with lifecycle._lock:
            lifecycle._closed = True
            lifecycle._stop.set()
            now = time.time()

            contact_id = lifecycle.contact_id
            lease_owner = lifecycle._lease_owner

            def mutate(
                state: dict[str, Any],
                *,
                contact_id: str = contact_id,
                lease_owner: str = lease_owner,
                now: float = now,
            ) -> None:
                entry = (state.get("contacts") or {}).get(
                    contact_id
                )
                if (
                    isinstance(entry, dict)
                    and entry.get("lease_owner") == lease_owner
                ):
                    entry["lease_until"] = now - 1
                    entry["lease_pid"] = 0

            update_json(LONG_TASK_STATE_FILE, _default_state(), mutate)
