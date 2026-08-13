"""Everything Silicon sends back: replies, progress, maintenance notices.

A reply may be several segments — text, a file, a voice note — and each one is
sent separately but reported as one delivery. Progress frames are best effort
by design: a Carbon losing a spinner is not worth failing a turn over.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from helpers import process as background
from helpers.session import SILICON, live_origins, resolve_rooms
from interface import client as client_module
from interface.constants import RICH_MEDIA_RE
from interface.contacts import get_own_profile
from interface.errors import CallBookkeepingError, InterfaceError
from interface.events import _event_id, _first_text
from interface.state import get_contact

_maintenance_notice_lock = threading.Lock()
_maintenance_notice_running = False


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


def _fan_out(send, empty_status: str) -> str:
    """Run ``send`` once per contact the live turn is answering.

    One session serves everyone, so a frame it did not address to anybody
    belongs to every room in the current turn — usually exactly one.
    """
    origins = live_origins()
    if not origins:
        return f"Error: {empty_status}"
    statuses = [f"{origin}: {send(origin)}" for origin in origins]
    return "; ".join(statuses)


def deliver_maintenance_notices(*, limit: int = 20) -> int:
    """Deliver durable, non-LLM maintenance acknowledgements to Carbons.

    The maintenance coordinator stores only one acknowledgement per contact
    and update.  A failed Interface call releases the claim for retry.
    """
    from silicon.runtime.maintenance import COORDINATOR

    delivered = 0
    client = client_module.InterfaceClient()
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
        from interface.work import (
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
    if contact_id == SILICON:
        # Not a room. Something the runtime generated rather than the session
        # addressing anybody — a timeout notice, a paused-work note — so it goes
        # to whoever the live turn is answering.
        return _fan_out(
            lambda origin: reply_contact(
                message,
                origin,
                work_continues=work_continues,
                progress_group_id=progress_group_id,
                client_id=client_id,
            ),
            "Nobody to reply to: this turn is answering no message.",
        )
    contact, err = _contact_room_or_error(contact_id)
    if err:
        return err
    assert contact is not None
    client = client_module.InterfaceClient()
    room_id = contact["room_id"]
    if not progress_group_id:
        try:
            from interface.work import current_manager_activity_group

            # The activity belongs to the turn, and the turn is the session's, so
            # it is stored under SILICON. Looking it up under the *destination*
            # found it back when there was a manager per contact; now it always
            # comes back empty, and the reply arrives detached from the spinner
            # that was running for it.
            progress_group_id = current_manager_activity_group(SILICON)
        except Exception:
            progress_group_id = ""
    errors: list[str] = []
    try:
        from diagnostics.store import Diagnostics

        # Same reason: one session, one active run, registered under SILICON.
        trace = Diagnostics.get_active_run(SILICON)
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
                        from diagnostics.journal import record_message
                        from iwantto.message_log import record_outbound

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
    rooms = {}
    for origin in resolve_rooms(contact_id):
        contact = get_contact(origin)
        if contact and contact.get("room_id"):
            rooms[origin] = str(contact["room_id"])
    if not rooms:
        return
    try:
        from interface.work import (
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
        # The frame's identity and revision are computed once, above, and the
        # same frame is delivered to every room in this turn — so a fanned-out
        # activity stays one activity rather than diverging per room.
        for origin, room_id in rooms.items():
            background.submit_best_effort(
                _deliver_progress,
                contact_id,
                room_id,
                group,
                state,
                message,
                frame_id,
                task_id,
                revision,
                occurred_at,
                progress_pct,
                summary,
                key=f"progress:{origin}:{group}:{frame_id}",
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
        client_module.InterfaceClient().progress(
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


