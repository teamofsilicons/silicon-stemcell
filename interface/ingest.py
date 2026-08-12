"""Turning one arrived event into something a manager can answer.

Media is downloaded, voice is transcribed, the durable work reference is
remembered, and the whole thing is rendered as the context a manager reads.
Bookkeeping happens before the manager runs so a crash mid-turn cannot lose
the fact that the message arrived.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

from helpers import process as background
from helpers.timefmt import now as _now
from helpers.timefmt import utc_iso as _utc_iso
from interface import client as client_module
from interface import constants
from interface.constants import (
    IGNORED_EVENT_TYPES,
    PROJECT_ROOT,
    USER_VISIBLE_EVENT_TYPES,
)
from interface.contacts import _contact_for_room
from interface.errors import CallBookkeepingError
from interface.events import (
    _event_body,
    _event_content,
    _event_display_time,
    _event_id,
    _event_is_self,
    _event_media_references,
    _event_reply_to,
    _event_room_id,
    _event_take_back_request_id,
    _event_type,
    _first_text,
)
from interface.state import _load_state, _save_state, state_serialized


@state_serialized
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


@state_serialized
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


@state_serialized
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
    client: client_module.InterfaceClient | None = None,
    filename: str = "",
) -> tuple[str, dict[str, Any]]:
    if not media_id:
        return "", {}
    client = client or client_module.InterfaceClient()
    info = client.media_show(media_id)
    if not isinstance(info, dict):
        return "", {}
    url = _first_text(info.get("download_url"), info.get("downloadUrl"), info.get("url"))
    if not url:
        return "", dict(info)
    if url.startswith("/"):
        try:
            from interface.config import load_config

            config, _ = load_config(PROJECT_ROOT)
            server_url = str(config.get("server_url") or "").rstrip("/")
            if server_url:
                url = server_url + url
        except Exception:
            return "", dict(info)
    chosen_name = _safe_filename(filename or info.get("filename") or info.get("name") or media_id)
    prefix = _safe_filename(event_id or str(int(_now() * 1000)))
    return _download_url(url, constants.MEDIA_DIR / f"{prefix}_{chosen_name}"), dict(info)


def _transcript_for_event(event: dict[str, Any], local_path: str, media_id: str, client: client_module.InterfaceClient) -> str:
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
        from iwantto.message_log import record_inbound

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
        from interface.work import (
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


def _send_read_receipt(client: client_module.InterfaceClient, room_id: str, event_id: str) -> None:
    try:
        client.read(room_id, event_id)
    except Exception:
        pass


def process_incoming_event(
    event: dict[str, Any],
    client: client_module.InterfaceClient | None = None,
    *,
    defer_processed_watermark: bool = False,
) -> tuple[str, str] | None:
    client = client or client_module.InterfaceClient()
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
                from interface.outbound import _record_sent_call_message

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
    background.submit_best_effort(
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
        background.submit_best_effort(
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


