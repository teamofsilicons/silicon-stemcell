import os
import json
import threading
import time
import uuid

from core.runtime_paths import DATA_ROOT
from core.state_store import file_lock, read_json, write_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
MANAGER_MESSAGES_FILE = os.path.join(PROJECT_ROOT, "core", "interface_state", "manager_queue.json")
_MANAGER_MESSAGES_LOCK = threading.RLock()
_MANAGER_DELIVERY_LOCK = threading.Lock()


def _load_manager_messages():
    with _MANAGER_MESSAGES_LOCK, file_lock(MANAGER_MESSAGES_FILE):
        value = read_json(MANAGER_MESSAGES_FILE, {})
        return value if isinstance(value, dict) else {}


def _save_manager_messages(messages):
    with _MANAGER_MESSAGES_LOCK, file_lock(MANAGER_MESSAGES_FILE):
        write_json(MANAGER_MESSAGES_FILE, messages)


def _diagnostic_envelope(from_contact_id, to_contact_id, target_type=""):
    """Capture the active trace without making manager messaging depend on it."""
    try:
        from core.diagnostics import Diagnostics

        trace = Diagnostics.get_active_run(from_contact_id)
        if trace is None:
            return {}
        handoff_id = uuid.uuid4().hex
        envelope = {
            "handoff_id": handoff_id,
            "source_run_id": trace.run_id,
            "room_id": trace.room_id,
            "message_ids": list(trace.message_ids),
            "target_type": str(target_type or ""),
            "target_id": str(to_contact_id or ""),
        }
        trace.event(
            "handoff.queued",
            handoff_id=handoff_id,
            target_type=envelope["target_type"],
            target_id=envelope["target_id"],
        )
        return envelope
    except Exception:
        return {}


def _record_inbound_work_call(contact_id, sender, message, work_call):
    try:
        from core.interface import get_contact
        from core.work_updates import enqueue_inbound_call

        sender_contact = get_contact(sender) or {}
        source_kind = (
            "silicon"
            if sender_contact.get("contact_type") == "silicon"
            else "manager"
        )
        enqueue_inbound_call(
            contact_id,
            source_kind=source_kind,
            source_id=sender,
            source_name=str(
                sender_contact.get("display_name")
                or sender_contact.get("name")
                or sender
            ),
            message=str(message or ""),
            outbound=work_call,
        )
    except Exception:
        pass


def _queue_lineage_handoff(
    from_contact_id,
    to_contact_id,
    message,
    diagnostics,
    work_call,
):
    """Use the task queue directly when this handoff has an active lineage."""
    activity = None
    try:
        from core.maintenance import COORDINATOR, current_activity

        if current_activity() is None:
            return False
        activity = COORDINATOR.acquire_activity(
            "manager_handoff",
            activity_id=uuid.uuid4().hex,
            contact_id=str(to_contact_id or ""),
        )
        if activity is None:
            return False
        parts = [
            f"Inter-manager messages:\nMessage from manager of "
            f"{from_contact_id or 'unknown'}:\n{message}"
        ]
        if isinstance(work_call, dict) and work_call:
            correlation = {
                "outbound_task_id": work_call.get("task_id"),
                "outbound_work_event_id": work_call.get("work_event_id"),
                "outbound_call_id": work_call.get("call_id"),
            }
            values = {key: value for key, value in correlation.items() if value}
            if values:
                parts.append(
                    "Work call correlation:\n"
                    + json.dumps(values, sort_keys=True)
                )
        queued = COORDINATOR.enqueue_continuation(
            str(to_contact_id),
            "\n---\n".join(parts),
            activity.reference(),
        )
        if not queued:
            COORDINATOR.release(activity)
            return False
        if isinstance(diagnostics, dict) and diagnostics:
            try:
                from core.diagnostics import Diagnostics

                Diagnostics.register_pending_context(to_contact_id, diagnostics)
            except Exception:
                pass
        if (
            isinstance(work_call, dict)
            and work_call
            and not work_call.get("continuation")
        ):
            try:
                from core.background import submit_best_effort

                submit_best_effort(
                    _record_inbound_work_call,
                    to_contact_id,
                    from_contact_id,
                    message,
                    dict(work_call),
                    key=f"manager-handoff-work:{to_contact_id}",
                )
            except Exception:
                pass
        return True
    except Exception:
        if activity is not None:
            try:
                COORDINATOR.release(activity)
            except Exception:
                pass
        return False


def send_manager_message(
    from_contact_id,
    to_contact_id,
    message,
    target_type="",
    work_call=None,
):
    """Queue a message from one manager to another and wake the dispatcher."""
    item = {
        "queue_id": uuid.uuid4().hex,
        "from_contact_id": from_contact_id,
        "message": message,
        "timestamp": time.time(),
    }
    diagnostics = _diagnostic_envelope(
        from_contact_id, to_contact_id, target_type=target_type
    )
    if diagnostics:
        item["diagnostics"] = diagnostics
    if isinstance(work_call, dict) and work_call:
        item["work_call"] = work_call
    if target_type:
        item["target_type"] = target_type

    if _queue_lineage_handoff(
        from_contact_id,
        to_contact_id,
        message,
        diagnostics,
        work_call,
    ):
        try:
            from core.interface import notify_runtime_activity

            notify_runtime_activity()
        except Exception:
            pass
        return "Done. Message queued for immediate delivery to the other manager."

    with _MANAGER_MESSAGES_LOCK, file_lock(MANAGER_MESSAGES_FILE):
        messages = _load_manager_messages()
        if not isinstance(messages.get(to_contact_id), list):
            messages[to_contact_id] = []
        messages[to_contact_id].append(item)
        _save_manager_messages(messages)
    try:
        from core.interface import notify_runtime_activity

        notify_runtime_activity()
    except Exception:
        pass
    return "Done. Message queued for immediate delivery to the other manager."


def check_manager_messages():
    """Drain one durable batch while allowing concurrent senders to append."""
    with _MANAGER_DELIVERY_LOCK, file_lock(f"{MANAGER_MESSAGES_FILE}.delivery"):
        return _check_manager_messages()


def _check_manager_messages():
    """Check for pending inter-manager messages. Returns {contact_id: context_string}."""
    # Snapshot under the file lock, then release it before diagnostics or
    # work-card bookkeeping. Items remain durable until formatting completes.
    with _MANAGER_MESSAGES_LOCK, file_lock(MANAGER_MESSAGES_FILE):
        messages = _load_manager_messages()
        if not messages:
            return {}
        upgraded = False
        for queued in messages.values():
            for item in queued if isinstance(queued, list) else []:
                if isinstance(item, dict) and not item.get("queue_id"):
                    item["queue_id"] = uuid.uuid4().hex
                    upgraded = True
        if upgraded:
            _save_manager_messages(messages)

    result = {}
    for contact_id, msgs in messages.items():
        if not msgs:
            continue
        parts = []
        for m in msgs:
            sender = m.get("from_contact_id") or m.get("from_carbon_id") or "unknown"
            work_call = m.get("work_call")
            inbound_call = {}
            if (
                isinstance(work_call, dict)
                and work_call
                and not work_call.get("continuation")
            ):
                try:
                    from core.interface import get_contact
                    from core.work_updates import enqueue_inbound_call

                    sender_contact = get_contact(sender) or {}
                    source_kind = (
                        "silicon"
                        if sender_contact.get("contact_type") == "silicon"
                        else "manager"
                    )
                    inbound_call = enqueue_inbound_call(
                        contact_id,
                        source_kind=source_kind,
                        source_id=sender,
                        source_name=str(
                            sender_contact.get("display_name")
                            or sender_contact.get("name")
                            or sender
                        ),
                        message=str(m.get("message") or ""),
                        outbound=work_call,
                    )
                except Exception:
                    inbound_call = {}
            parts.append(f"Message from manager of {sender}:\n{m['message']}")
            if isinstance(work_call, dict) and work_call:
                correlation = {
                    "outbound_task_id": work_call.get("task_id"),
                    "outbound_work_event_id": work_call.get("work_event_id"),
                    "outbound_call_id": work_call.get("call_id"),
                    "inbound_task_id": inbound_call.get("task_id"),
                    "inbound_work_event_id": inbound_call.get("work_event_id"),
                    "inbound_call_id": inbound_call.get("call_id"),
                }
                parts.append(
                    "Work call correlation:\n"
                    + json.dumps(
                        {key: value for key, value in correlation.items() if value},
                        sort_keys=True,
                    )
                )
            diagnostics = m.get("diagnostics")
            if isinstance(diagnostics, dict):
                try:
                    from core.diagnostics import Diagnostics

                    Diagnostics.register_pending_context(contact_id, diagnostics)
                except Exception:
                    pass
        result[contact_id] = "Inter-manager messages:\n" + "\n---\n".join(parts)

    delivered_ids = {
        str(item.get("queue_id") or "")
        for queued in messages.values()
        for item in (queued if isinstance(queued, list) else [])
        if isinstance(item, dict) and item.get("queue_id")
    }
    with _MANAGER_MESSAGES_LOCK, file_lock(MANAGER_MESSAGES_FILE):
        current = _load_manager_messages()
        for contact_id, queued in list(current.items()):
            if not isinstance(queued, list):
                continue
            remaining = [
                item
                for item in queued
                if not (
                    isinstance(item, dict)
                    and str(item.get("queue_id") or "") in delivered_ids
                )
            ]
            if remaining:
                current[contact_id] = remaining
            else:
                current.pop(contact_id, None)
        _save_manager_messages(current)

    return result
