"""Keeping the inbox read live, and waking the event loop when it moves.

A long manager turn must never stall ingestion, so a listener thread owns the
inbox read and hands complete records to a queue. A second thread watches the
runtime state files that other processes write, so a change there wakes the
loop instead of waiting out its recovery tick.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from helpers import process as background
from helpers.timefmt import now as _now
from helpers.timefmt import utc_iso as _utc_iso
from helpers.watch import PathChangeWaiter, PathSetChangeWaiter
from interface import client as client_module
from interface import constants
from interface import contacts as contacts_module
from interface import ingest as ingest_module
from interface.constants import (
    DAEMON_DEEP_HEALTH_JITTER_SECONDS,
    DAEMON_DEEP_HEALTH_SECONDS,
    DAEMON_HEALTH_SECONDS,
    INBOX_POLL_SECONDS,
    PROJECT_ROOT,
    RUNTIME_FILE_POLL_SECONDS,
)
from interface.contacts import _room_id
from interface.errors import CallBookkeepingError, DurableHandoffError, InterfaceError
from interface.events import _event_id, _event_room_id
from interface.inbox_file import _commit_inbox_record, _read_new_inbox_records
from interface.ingest import _remember_processed
from interface.models import InboxRecord
from interface.state import _load_state, _save_state, state_serialized

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
            client = client_module.InterfaceClient()
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
            inbox_path = Path(inbox_value).expanduser() if inbox_value else constants.DEFAULT_INBOX_FILE
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


@state_serialized
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


def _schedule_room_refresh(client: client_module.InterfaceClient) -> None:
    """Collapse bursts of room invalidations into one background refresh."""
    background.submit_best_effort(
        contacts_module.discover_rooms,
        client,
        force=True,
        key="interface:room-refresh",
        coalesce=True,
    )


def _reconcile_durable_frame(frame: dict[str, Any], client: client_module.InterfaceClient) -> None:
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
        from interface.work import replay_pending_call_updates

        replay_pending_call_updates()
    except Exception as exc:
        print(f"[Work updates] call retry scheduling failed: {exc}", flush=True)
    client = client_module.InterfaceClient()
    try:
        contacts_module.discover_rooms(client)
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
                    processed = ingest_module.process_incoming_event(
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


