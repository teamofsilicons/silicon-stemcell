"""Durable queue for manager-to-manager messages.

Messages between silicons outlive the sending turn, so they are journalled to
``manager_queue.json`` under a lock rather than delivered in-process.  The
queue is bounded (``MANAGER_QUEUE_MAX_ITEMS``), carries diagnostic lineage so a
handoff can be correlated across runs, and reports capacity/conflict problems
through ``ManagerQueueCapacityError`` / ``ManagerQueueConflictError``.
"""
import os
import json
import threading
import time
import uuid

from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.state import file_lock, read_json, write_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
MANAGER_MESSAGES_FILE = os.path.join(os.fspath(STATE_DIR), "manager_queue.json")
_MANAGER_MESSAGES_LOCK = threading.RLock()
_MANAGER_DELIVERY_LOCK = threading.Lock()
MANAGER_QUEUE_MAX_ITEMS = 1_000
MANAGER_QUEUE_META_KEY = "__silicon_manager_queue_meta_v1__"
_MAX_HEALTH_COUNTER = (2**63) - 1


class ManagerQueueCapacityError(RuntimeError):
    """Body-free signal that no new manager handoff can be accepted."""


class ManagerQueueConflictError(RuntimeError):
    """Body-free signal that a queue identity was reused inconsistently."""


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
        from diagnostics.store import Diagnostics

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


def _append_manager_queue_item(contact_id, item):
    """Commit one delivery/reconciliation record before acknowledging it."""
    with _MANAGER_MESSAGES_LOCK, file_lock(MANAGER_MESSAGES_FILE):
        messages = _load_manager_messages()
        queue_id = str(item.get("queue_id") or "")
        if queue_id:
            for key, queued in messages.items():
                if key == MANAGER_QUEUE_META_KEY or not isinstance(queued, list):
                    continue
                for value in queued:
                    if (
                        isinstance(value, dict)
                        and str(value.get("queue_id") or "") == queue_id
                    ):
                        if key == contact_id and value == item:
                            return
                        raise ManagerQueueConflictError(
                            "Manager message queue identity conflicts with an "
                            "accepted item."
                        )
        queued_count = sum(
            len(queued)
            for key, queued in messages.items()
            if key != MANAGER_QUEUE_META_KEY and isinstance(queued, list)
        )
        if queued_count >= MANAGER_QUEUE_MAX_ITEMS:
            meta = messages.get(MANAGER_QUEUE_META_KEY)
            if not isinstance(meta, dict):
                meta = {}
                messages[MANAGER_QUEUE_META_KEY] = meta
            meta["overflow_count"] = min(
                _MAX_HEALTH_COUNTER,
                max(0, int(meta.get("overflow_count") or 0)) + 1,
            )
            meta["last_overflow_at"] = time.time()
            _save_manager_messages(messages)
            raise ManagerQueueCapacityError(
                "Manager message queue is at its live-entry limit."
            )
        if not isinstance(messages.get(contact_id), list):
            messages[contact_id] = []
        messages[contact_id].append(item)
        _save_manager_messages(messages)


def _remove_manager_queue_ids(queue_ids):
    queue_ids = {str(value or "") for value in queue_ids if value}
    if not queue_ids:
        return
    with _MANAGER_MESSAGES_LOCK, file_lock(MANAGER_MESSAGES_FILE):
        messages = _load_manager_messages()
        for contact_id, queued in list(messages.items()):
            if not isinstance(queued, list):
                continue
            remaining = [
                item
                for item in queued
                if not (
                    isinstance(item, dict)
                    and str(item.get("queue_id") or "") in queue_ids
                )
            ]
            if remaining:
                messages[contact_id] = remaining
            else:
                messages.pop(contact_id, None)
        _save_manager_messages(messages)


def manager_queue_health():
    """Return a bounded, body-free manager handoff queue summary."""
    with _MANAGER_MESSAGES_LOCK, file_lock(MANAGER_MESSAGES_FILE):
        messages = _load_manager_messages()
        queued = sum(
            len(values)
            for key, values in messages.items()
            if key != MANAGER_QUEUE_META_KEY and isinstance(values, list)
        )
        meta = messages.get(MANAGER_QUEUE_META_KEY)
        if not isinstance(meta, dict):
            meta = {}
        return {
            "queued": min(_MAX_HEALTH_COUNTER, max(0, queued)),
            "capacity": MANAGER_QUEUE_MAX_ITEMS,
            "overflow_count": min(
                _MAX_HEALTH_COUNTER,
                max(0, int(meta.get("overflow_count") or 0)),
            ),
            "last_overflow_at": max(
                0.0,
                float(meta.get("last_overflow_at") or 0.0),
            ),
        }


def _ensure_manager_work_calls(contact_id, item):
    """Idempotently journal both visible call sides for one accepted handoff."""
    work_call = item.get("work_call")
    if not isinstance(work_call, dict) or not work_call:
        return {}

    from interface import get_contact
    from interface.work import enqueue_inbound_call, enqueue_outbound_call

    queue_id = str(item.get("queue_id") or "")
    if not queue_id:
        raise ValueError("Manager call delivery requires queue_id.")
    sender = str(
        item.get("from_contact_id")
        or item.get("from_carbon_id")
        or "unknown"
    )
    message = str(item.get("message") or "")
    target_contact = get_contact(contact_id) or {}
    target_display = str(
        target_contact.get("display_name")
        or target_contact.get("name")
        or contact_id
    )
    target_name = (
        target_display
        if str(work_call.get("target_kind") or "") == "silicon"
        else f"{target_display}'s manager"
    )
    outbound_accepted = enqueue_outbound_call(
        work_call,
        target_name=target_name,
        message=message,
        idempotency_key=f"manager-handoff:{queue_id}:outbound",
    )
    if not outbound_accepted:
        raise RuntimeError("Manager call bookkeeping was not accepted.")

    if work_call.get("continuation"):
        return {}
    sender_contact = get_contact(sender) or {}
    source_kind = (
        "silicon"
        if sender_contact.get("contact_type") == "silicon"
        else "manager"
    )
    source_display = str(
        sender_contact.get("display_name")
        or sender_contact.get("name")
        or sender
    )
    return enqueue_inbound_call(
        contact_id,
        source_kind=source_kind,
        source_id=sender,
        source_name=(
            source_display
            if source_kind == "silicon"
            else f"{source_display}'s manager"
        ),
        message=message,
        outbound=work_call,
        idempotency_key=f"manager-handoff:{queue_id}:inbound",
    )


def _resume_prepared_lineage(contact_id, item):
    """Idempotently commit or verify a prepared maintenance continuation."""
    from manager.runtime.maintenance import COORDINATOR

    return COORDINATOR.enqueue_continuation(
        str(contact_id),
        str(item.get("lineage_context") or ""),
        dict(item.get("lineage_activity_reference") or {}),
        queue_id=str(item.get("queue_id") or ""),
    )


def _queue_lineage_handoff(
    from_contact_id,
    to_contact_id,
    message,
    diagnostics,
    work_call,
    queue_item=None,
):
    """Use the task queue directly when this handoff has an active lineage."""
    activity = None
    reconciliation = None
    try:
        from manager.runtime.maintenance import COORDINATOR, current_activity

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
        context = "\n---\n".join(parts)
        if isinstance(work_call, dict) and work_call:
            reconciliation = dict(queue_item or {})
            reconciliation["queue_id"] = str(
                reconciliation.get("queue_id") or uuid.uuid4().hex
            )
            reconciliation["from_contact_id"] = from_contact_id
            reconciliation["message"] = message
            reconciliation["work_call"] = dict(work_call)
            reconciliation["reconcile_only"] = True
            reconciliation["lineage_prepared"] = True
            reconciliation["lineage_context"] = context
            reconciliation["lineage_activity_reference"] = (
                activity.reference()
            )
            # The prepared record is the transaction intent. It must exist
            # before the coordinator can accept the primary manager turn.
            _append_manager_queue_item(to_contact_id, reconciliation)
        try:
            queued = COORDINATOR.enqueue_continuation(
                str(to_contact_id),
                context,
                activity.reference(),
                queue_id=str(
                    (reconciliation or queue_item or {}).get("queue_id") or ""
                ),
            )
        except Exception:
            if reconciliation is not None:
                # Acceptance is ambiguous, so never enqueue a second manager
                # turn. The prepared record can verify the same queue_id after
                # restart and either finish or safely fall back.
                print(
                    "[Messages] manager lineage acceptance deferred",
                    flush=True,
                )
                return True
            raise
        if not queued:
            if reconciliation is not None:
                _remove_manager_queue_ids([reconciliation["queue_id"]])
            COORDINATOR.release(activity)
            return False
        if isinstance(diagnostics, dict) and diagnostics:
            try:
                from diagnostics.store import Diagnostics

                Diagnostics.register_pending_context(to_contact_id, diagnostics)
            except Exception:
                pass
        if reconciliation is not None:
            try:
                _ensure_manager_work_calls(to_contact_id, reconciliation)
                _remove_manager_queue_ids([reconciliation["queue_id"]])
            except Exception:
                # The reconciliation record remains for the next drain.
                # Avoid printing exception text because it may contain a
                # transcript-bearing CLI command.
                print(
                    "[Messages] manager call reconciliation deferred",
                    flush=True,
                )
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
    sender_label="",
):
    """Queue a message from one manager to another and wake the dispatcher.

    ``sender_label`` names the sender verbatim for senders that are not another
    contact's manager — a worker reporting to its own manager, or a previous
    session handing over its first message.
    """
    item = {
        "queue_id": uuid.uuid4().hex,
        "from_contact_id": from_contact_id,
        "message": message,
        "timestamp": time.time(),
    }
    if sender_label:
        item["sender_label"] = str(sender_label)
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
        item,
    ):
        try:
            from interface import notify_runtime_activity

            notify_runtime_activity()
        except Exception:
            pass
        return "Done. Message queued for immediate delivery to the other manager."

    try:
        _append_manager_queue_item(to_contact_id, item)
    except ManagerQueueCapacityError:
        return "Error: manager message queue is at capacity; message was not accepted."
    if isinstance(work_call, dict) and work_call:
        try:
            _ensure_manager_work_calls(to_contact_id, item)
        except Exception:
            # The accepted manager queue item is the durable reconciliation
            # source. It must remain until a later drain journals both cards.
            print(
                "[Messages] manager call bookkeeping deferred",
                flush=True,
            )
    try:
        from interface import notify_runtime_activity
        from diagnostics.journal import record_message

        record_message(
            "out",
            to_contact_id,
            via="manager_queue",
            sender=sender_label or from_contact_id,
            body=message,
        )
        notify_runtime_activity()
    except Exception:
        pass
    return "Done. Message queued for immediate delivery to the other manager."


def check_manager_messages():
    """Drain one durable batch while allowing concurrent senders to append."""
    with _MANAGER_DELIVERY_LOCK, file_lock(f"{MANAGER_MESSAGES_FILE}.delivery"):
        return _check_manager_messages()


def check_manager_messages_durable():
    """Transfer queued messages to durable manager roots before source ack."""
    with _MANAGER_DELIVERY_LOCK, file_lock(f"{MANAGER_MESSAGES_FILE}.delivery"):
        return _check_manager_messages(durable_handoff=True)


def _check_manager_messages(*, durable_handoff=False):
    """Check for pending inter-manager messages. Returns {contact_id: context_string}."""
    # Snapshot under the file lock, then release it before diagnostics or
    # work-card bookkeeping. Items remain durable until formatting completes.
    with _MANAGER_MESSAGES_LOCK, file_lock(MANAGER_MESSAGES_FILE):
        messages = _load_manager_messages()
        if not messages:
            return {}
        upgraded = False
        for key, queued in messages.items():
            if key == MANAGER_QUEUE_META_KEY:
                continue
            for item in queued if isinstance(queued, list) else []:
                if isinstance(item, dict) and not item.get("queue_id"):
                    item["queue_id"] = uuid.uuid4().hex
                    upgraded = True
        if upgraded:
            _save_manager_messages(messages)

    result = {}
    delivered_ids = set()
    for contact_id, msgs in messages.items():
        if contact_id == MANAGER_QUEUE_META_KEY or not isinstance(msgs, list) or not msgs:
            continue
        parts = []
        for m in msgs:
            if m.get("lineage_prepared"):
                try:
                    lineage_accepted = _resume_prepared_lineage(
                        contact_id,
                        m,
                    )
                except Exception:
                    break
                if not lineage_accepted:
                    # The prepared intent was never accepted (for example, its
                    # source lease expired before transfer). Deliver it through
                    # the normal durable manager queue exactly once.
                    m = dict(m)
                    m["reconcile_only"] = False
            sender = m.get("from_contact_id") or m.get("from_carbon_id") or "unknown"
            work_call = m.get("work_call")
            inbound_call = {}
            if isinstance(work_call, dict) and work_call:
                try:
                    inbound_call = _ensure_manager_work_calls(contact_id, m)
                except Exception:
                    # Preserve per-recipient FIFO ordering and leave this item
                    # plus every later item durable for the next pass.
                    break
            queue_id = str(m.get("queue_id") or "")
            if m.get("reconcile_only"):
                if queue_id:
                    delivered_ids.add(queue_id)
                continue
            sender_label = str(m.get("sender_label") or "")
            item_parts = [
                f"Message from {sender_label or f'manager of {sender}'}:"
                f"\n{m['message']}"
            ]
            if isinstance(work_call, dict) and work_call:
                correlation = {
                    "outbound_task_id": work_call.get("task_id"),
                    "outbound_work_event_id": work_call.get("work_event_id"),
                    "outbound_call_id": work_call.get("call_id"),
                    "inbound_task_id": inbound_call.get("task_id"),
                    "inbound_work_event_id": inbound_call.get("work_event_id"),
                    "inbound_call_id": inbound_call.get("call_id"),
                }
                item_parts.append(
                    "Work call correlation:\n"
                    + json.dumps(
                        {key: value for key, value in correlation.items() if value},
                        sort_keys=True,
                    )
                )
            if durable_handoff:
                try:
                    from manager.runtime.maintenance import COORDINATOR

                    accepted = COORDINATOR.enqueue_ingress_root(
                        str(contact_id),
                        "Inter-manager messages:\n"
                        + "\n---\n".join(item_parts),
                        ingress_id=f"manager:{queue_id}",
                    )
                except Exception:
                    # The source item remains durable. Preserve recipient FIFO
                    # order and retry the same ingress identity next pass.
                    break
                if not accepted:
                    break
            else:
                parts.extend(item_parts)
            diagnostics = m.get("diagnostics")
            if isinstance(diagnostics, dict):
                try:
                    from diagnostics.store import Diagnostics

                    Diagnostics.register_pending_context(contact_id, diagnostics)
                except Exception:
                    pass
            if queue_id:
                delivered_ids.add(queue_id)
        if durable_handoff:
            continue
        if parts:
            result[contact_id] = (
                "Inter-manager messages:\n" + "\n---\n".join(parts)
            )

    _remove_manager_queue_ids(delivered_ids)

    return result
