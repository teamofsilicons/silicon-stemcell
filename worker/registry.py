"""Everything Silicon knows about its workers, and every run they produced.

Three JSON documents and one log directory:

    _worker_registry.json  one durable record per worker, across every run
    _active_workers.json   the live process table
    _archive_meta.json     the index of finished runs

Reads are migrated in place — an older Stemcell wrote ``session_uuid`` and
``chatgpt`` — and writes mutate the stored dict rather than replacing it, so a
key this version does not know about survives.

Nothing is ever deleted. An archived run stays for as long as the instance does.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

from helpers.state import file_lock, read_json, update_json, write_json
from worker import constants
from worker.leases import _maintenance_reference



def _number_or_zero(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --- State persistence ---

def _load_active():
    value = read_json(constants.ACTIVE_FILE, {})
    return value if isinstance(value, dict) else {}


def _save_active(active):
    write_json(constants.ACTIVE_FILE, active)


def _load_browser_queue():
    value = read_json(constants.BROWSER_QUEUE_FILE, [])
    return value if isinstance(value, list) else []


def _save_browser_queue(queue):
    write_json(constants.BROWSER_QUEUE_FILE, queue)


def _load_archive_meta():
    value = read_json(constants.ARCHIVE_META_FILE, {})
    return value if isinstance(value, dict) else {}


def _migrate_worker_record(worker_id, record):
    changed = False

    if "session_uuid" in record and "session_id" not in record:
        record["session_id"] = record.pop("session_uuid")
        changed = True

    if "provider" not in record:
        if record.get("worker_type") in ("browser", "writer"):
            record["provider"] = "claude"
            changed = True
        elif record.get("session_id"):
            record["provider"] = "claude"
            changed = True
    elif record.get("provider") == "chatgpt":
        record["provider"] = "codex"
        changed = True

    if "worker_id" not in record:
        record["worker_id"] = worker_id
        changed = True

    return record, changed


def _load_worker_registry():
    with file_lock(constants.WORKER_REGISTRY_FILE):
        registry = read_json(constants.WORKER_REGISTRY_FILE, {})
        if not isinstance(registry, dict):
            return {}

        changed = False
        for worker_id, record in registry.items():
            _, record_changed = _migrate_worker_record(worker_id, record)
            changed = changed or record_changed

        if changed:
            _save_worker_registry(registry)

        return registry


def _save_worker_registry(registry):
    write_json(constants.WORKER_REGISTRY_FILE, registry)


def _remove_worker_record(worker_id):
    def remove(registry):
        if isinstance(registry, dict):
            registry.pop(worker_id, None)

    update_json(constants.WORKER_REGISTRY_FILE, {}, remove)


def _worker_launch_lock_path(worker_id):
    safe_id = "".join(
        char if char.isalnum() or char in "-_." else "-"
        for char in str(worker_id or "worker")
    )[:80]
    return os.path.join(constants.WORKER_STATE_DIR, f".launch-{safe_id}.json")


def _remove_active_worker(worker_id, expected_run_id="", expected_claim_token=""):
    removed = {}

    def remove(active):
        if not isinstance(active, dict):
            return
        current = active.get(worker_id)
        if not isinstance(current, dict):
            return
        if expected_run_id and str(current.get("run_id") or "") != str(expected_run_id):
            return
        if (
            expected_claim_token
            and str(current.get("_completion_claim_token") or "")
            != str(expected_claim_token)
        ):
            return
        removed.update(current)
        active.pop(worker_id, None)

    update_json(constants.ACTIVE_FILE, {}, remove)
    return removed


def _claim_completed_worker(worker_id, expected_run_id=""):
    claimed = {}
    claim_token = uuid.uuid4().hex
    now = time.time()

    def claim(active):
        if not isinstance(active, dict):
            return
        current = active.get(worker_id)
        if not isinstance(current, dict):
            return
        if expected_run_id and str(current.get("run_id") or "") != str(expected_run_id):
            return
        claimed_at = float(current.get("_completion_claimed_at") or 0)
        if claimed_at and now - claimed_at < 60:
            return
        current["_completion_claimed_at"] = now
        current["_completion_claim_token"] = claim_token
        claimed.update(current)

    update_json(constants.ACTIVE_FILE, {}, claim)
    return claimed


def _utc_timestamp_slug():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _run_output_path(worker_id, run_id):
    return os.path.join(constants.OUTPUTS_DIR, f"{worker_id}-{run_id}.log")


def _make_archive_id(worker_id, run_id):
    return f"{worker_id}-{run_id}"


def _get_worker_record(worker_id):
    return _load_worker_registry().get(worker_id)


def _create_worker_record(worker_id, worker_type, carbon_id, incognito=False):
    now = time.time()
    record = {
        "worker_id": worker_id,
        "worker_type": worker_type,
        "carbon_id": carbon_id,
        "created_at": now,
        "last_used_at": now,
        "last_run_id": "",
        "last_archive_id": "",
        "incognito": incognito,
        "provider": "",
        "session_id": "",
    }
    created = False

    def create(registry):
        nonlocal created
        if not isinstance(registry, dict) or worker_id in registry:
            return
        registry[worker_id] = record
        created = True

    update_json(constants.WORKER_REGISTRY_FILE, {}, create)
    if not created:
        return None, f"Error: Worker '{worker_id}' already exists. Use worker/message to prompt it again."
    return record, ""


def _update_worker_record(worker_id, **updates):
    def update(registry):
        if isinstance(registry, dict) and worker_id in registry:
            registry[worker_id].update(updates)

    update_json(constants.WORKER_REGISTRY_FILE, {}, update)



def _archive_active_output(worker_id, worker_info, carbon_id):
    output_path = worker_info.get("output_path")
    if not output_path or not os.path.exists(output_path):
        return ""

    run_id = worker_info.get("run_id") or _utc_timestamp_slug()
    archive_id = _make_archive_id(worker_id, run_id)
    archive_path = os.path.join(constants.OUTPUTS_DIR, f"{archive_id}.log")

    if os.path.abspath(output_path) != os.path.abspath(archive_path):
        os.rename(output_path, archive_path)

    archive_record = {
        "worker_id": worker_id,
        "run_id": run_id,
        "provider": worker_info.get("provider", ""),
        "session_id": worker_info.get("session_id", ""),
        "carbon_id": carbon_id,
        "worker_type": worker_info.get("worker_type", "unknown"),
        "task": worker_info.get("task", ""),
        "started_at": worker_info.get("started"),
        "archived_at": time.time(),
        "incognito": worker_info.get("incognito", False),
    }

    def remember(meta):
        if isinstance(meta, dict):
            meta[archive_id] = archive_record

    update_json(constants.ARCHIVE_META_FILE, {}, remember)
    return archive_id


# --- Internal helpers ---


def _record_active_run(worker_id, provider, session_id, process, task, worker_type, carbon_id, output_path, incognito, run_id):
    diag_parent_run_id = ""
    diag_room_id = ""
    diag_message_ids = []
    try:
        from diagnostics.store import Diagnostics
        parent_trace = Diagnostics.get_active_run(carbon_id)
        if parent_trace:
            diag_parent_run_id = parent_trace.run_id
            diag_room_id = parent_trace.room_id
            diag_message_ids = list(parent_trace.message_ids)
    except Exception:
        pass
    active_record = {
        "pid": process.pid,
        "started": time.time(),
        "task": task,
        "worker_type": worker_type,
        "carbon_id": carbon_id,
        "output_path": output_path,
        "incognito": incognito,
        "provider": provider,
        "session_id": session_id,
        "run_id": run_id,
        "diag_parent_run_id": diag_parent_run_id,
        "diag_room_id": diag_room_id,
        "diag_message_ids": diag_message_ids,
        "maintenance_activity": _maintenance_reference(),
    }

    def remember_active(active):
        if isinstance(active, dict):
            active[worker_id] = active_record

    update_json(constants.ACTIVE_FILE, {}, remember_active)
    _update_worker_record(
        worker_id,
        provider=provider,
        session_id=session_id,
        last_used_at=time.time(),
        last_run_id=run_id,
        incognito=incognito,
    )
