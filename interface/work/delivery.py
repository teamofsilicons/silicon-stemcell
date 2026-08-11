"""Getting a call update to the other side, and retrying until it lands.

Delivery is lane-ordered: one call's updates arrive in the order they were
made, and a failure holds its own lane rather than the whole queue.
"""
from __future__ import annotations

from interface.work import constants
from interface.work import cache as cache_module
from interface.work import correlation as correlation_module
from interface.work import identity as identity_module
from interface.work import journal as journal_module
from interface.work import payloads as payloads_module
from interface.work import retry as retry_module
from interface.work import store as store_module
import time
from copy import deepcopy
from typing import Any
from helpers.process import submit_best_effort
from interface import (
    InterfaceClient,
    WorkCallMutationError,
)


def _call_mutation_satisfied(
    snapshot: dict[str, Any],
    payload: dict[str, Any],
) -> bool:
    if not snapshot:
        return False
    for field in ("state", "body", "blocks"):
        if field in payload and snapshot.get(field) != payload[field]:
            return False
    desired_transcript = payload.get("transcript")
    if desired_transcript:
        current = {
            str(row.get("transcript_id") or ""): row
            for row in snapshot.get("transcript") or []
            if isinstance(row, dict)
        }
        for desired in desired_transcript:
            if not isinstance(desired, dict):
                return False
            existing = current.get(str(desired.get("transcript_id") or ""))
            if not isinstance(existing, dict):
                return False
            if any(existing.get(key) != value for key, value in desired.items()):
                return False
    return True


def _deliver_call_patch(
    transport: InterfaceClient,
    reference: dict[str, Any],
    payload: dict[str, Any],
) -> Any:
    """Resolve optimistic revision at attempt time and reconcile replays."""
    task_id = str(reference.get("task_id") or "")
    call_id = str(reference.get("call_id") or "")
    snapshot = cache_module._cached_call_snapshot(reference)
    if _call_mutation_satisfied(snapshot, payload):
        return snapshot
    attempt_payload = deepcopy(payload)
    revision = snapshot.get("revision") if isinstance(snapshot, dict) else None
    if isinstance(revision, int) and not isinstance(revision, bool):
        attempt_payload["revision"] = revision

    def patch() -> Any:
        if task_id:
            return transport.work_call_patch(
                task_id,
                call_id,
                attempt_payload,
            )
        return transport.work_standalone_call_patch(call_id, attempt_payload)

    try:
        return patch()
    except WorkCallMutationError as exc:
        if exc.status_code != 409 or exc.current_revision is None:
            raise
        # A lost success leaves the cached revision stale. Re-apply the exact
        # semantic mutation at the authoritative revision; stable transcript
        # ids/timestamps make this content-idempotent.
        attempt_payload["revision"] = exc.current_revision
        return patch()


def _deliver_call_retry(
    retry_id: str,
    *,
    client: InterfaceClient | None = None,
    claim_now: float | None = None,
) -> Any:
    """Attempt one claimed call mutation and retain it on any failure."""
    entry = retry_module._claim_call_retry(retry_id, now=claim_now)
    if not entry:
        with retry_module._CALL_RETRY_LOCK:
            retry_module._CALL_RETRY_INFLIGHT.discard(retry_id)
        return False
    reference = dict(entry.get("reference") or {})
    task_id = str(reference.get("task_id") or "")
    call_id = str(reference.get("call_id") or "")
    payload = deepcopy(entry.get("payload") or {})
    try:
        if not call_id or not payload:
            raise constants.WorkUpdateError("Journaled call mutation is incomplete.")
        transport = client or InterfaceClient()
        if entry.get("operation") == "create" and task_id:
            result = transport.work_call_create(task_id, payload)
        elif entry.get("operation") == "create":
            result = transport.work_standalone_call_create(payload)
        elif entry.get("operation") == "patch":
            result = _deliver_call_patch(transport, reference, payload)
        else:
            raise constants.WorkUpdateError("Journaled call mutation has no operation.")
        snapshot = identity_module._result_data(result)
        if snapshot:
            cache_module._remember_event(
                str(reference.get("owner_contact_id") or ""),
                snapshot,
            )
    except Exception as exc:
        retry_module._record_call_retry_failure(retry_id, entry, exc)
        failed = journal_module._call_retry_entry(retry_id)
        if (
            failed.get("status") == "dead_letter"
            and failed.get("operation") != "create"
        ):
            _schedule_next_call_lane(failed, client=client)
        return False
    else:
        removed = retry_module._complete_call_retry(retry_id, entry)
        if removed:
            correlation_module._clear_call_correlations(call_id)
            _schedule_next_call_lane(entry, client=client)
        return result
    finally:
        with retry_module._CALL_RETRY_LOCK:
            retry_module._CALL_RETRY_INFLIGHT.discard(retry_id)


def _schedule_call_retry(
    retry_id: str,
    *,
    client: InterfaceClient | None = None,
    claim_now: float | None = None,
) -> bool:
    with retry_module._CALL_RETRY_LOCK:
        if retry_id in retry_module._CALL_RETRY_INFLIGHT:
            return True
        retry_module._CALL_RETRY_INFLIGHT.add(retry_id)
    entry = journal_module._call_retry_entry(retry_id)
    lane = str(
        entry.get("lane")
        or payloads_module._call_retry_lane(entry.get("reference") or {})
    )
    accepted = submit_best_effort(
        _deliver_call_retry,
        retry_id,
        client=client,
        claim_now=claim_now,
        key=f"work-call-retry:{lane}",
    )
    if not accepted:
        with retry_module._CALL_RETRY_LOCK:
            retry_module._CALL_RETRY_INFLIGHT.discard(retry_id)
    return accepted


def _schedule_next_call_lane(
    completed: dict[str, Any],
    *,
    client: InterfaceClient | None = None,
) -> bool:
    lane = str(
        completed.get("lane")
        or payloads_module._call_retry_lane(completed.get("reference") or {})
    )
    now = time.time()
    with store_module._state_guard():
        state = store_module._read_state()
        candidates = sorted(
            (
                entry
                for entry in state.get("call_retry_journal", {}).values()
                if isinstance(entry, dict)
                and str(
                    entry.get("lane")
                    or payloads_module._call_retry_lane(entry.get("reference") or {})
                )
                == lane
                and (
                    entry.get("status", "pending") == "pending"
                    or (
                        entry.get("status") == "dead_letter"
                        and entry.get("operation") == "create"
                    )
                )
            ),
            key=lambda entry: (
                int(entry.get("sequence") or 0),
                float(entry.get("created_at") or 0.0),
                str(entry.get("retry_id") or ""),
            ),
        )
    if not candidates:
        return False
    next_entry = candidates[0]
    if (
        next_entry.get("status", "pending") != "pending"
        or float(next_entry.get("next_attempt_at") or 0.0) > now
    ):
        return False
    return _schedule_call_retry(
        str(next_entry.get("retry_id") or ""),
        client=client,
    )


def replay_pending_call_updates(
    *,
    limit: int = constants.CALL_RETRY_BATCH_LIMIT,
    now: float | None = None,
    client: InterfaceClient | None = None,
) -> int:
    """Schedule due disk-journaled call cards after startup or a periodic tick."""
    now = time.time() if now is None else float(now)
    with store_module._state_guard():
        state, pruned = store_module._read_state_with_prune_status()
        if pruned:
            store_module._write_state(state)
        journal = state.get("call_retry_journal", {})
        ordered = sorted(
            (entry for entry in journal.values() if isinstance(entry, dict)),
            key=lambda entry: (
                int(entry.get("sequence") or 0),
                float(entry.get("created_at") or 0.0),
                str(entry.get("retry_id") or ""),
            ),
        )
        first_by_lane: dict[str, dict[str, Any]] = {}
        for entry in ordered:
            if not (
                entry.get("status", "pending") == "pending"
                or (
                    entry.get("status") == "dead_letter"
                    and entry.get("operation") == "create"
                )
            ):
                continue
            lane = str(
                entry.get("lane")
                or payloads_module._call_retry_lane(entry.get("reference") or {})
            )
            first_by_lane.setdefault(lane, entry)
        eligible = [
            entry
            for entry in first_by_lane.values()
            if entry.get("status", "pending") == "pending"
            and float(entry.get("next_attempt_at") or 0.0) <= now
            and float(entry.get("lease_expires_at") or 0.0) <= now
        ][: max(0, int(limit))]
    scheduled = 0
    for entry in eligible:
        retry_id = str(entry.get("retry_id") or "")
        if retry_id and _schedule_call_retry(
            retry_id,
            client=client,
            claim_now=now,
        ):
            scheduled += 1
    return scheduled


def pending_call_update_retries(
    *,
    persist_prune: bool = True,
) -> dict[str, Any]:
    """Return body-free retry health for diagnostics and runtime probes."""
    with store_module._state_guard():
        state, pruned = store_module._read_state_with_prune_status()
        if persist_prune and pruned:
            store_module._write_state(state)
        journal = state.get("call_retry_journal", {})
        entries = [entry for entry in journal.values() if isinstance(entry, dict)]
        archived = state.get("call_retry_dead_letters")
        archived_count = len(archived) if isinstance(archived, list) else 0
        overflow_count = int(state.get("call_retry_overflow_count") or 0)
        last_overflow_at = float(
            state.get("call_retry_last_overflow_at") or 0.0
        )
    pending = [
        entry for entry in entries if entry.get("status", "pending") == "pending"
    ]
    dead_letter = [
        entry for entry in entries if entry.get("status") == "dead_letter"
    ]
    return {
        "pending": len(pending),
        "failed": sum(
            1 for entry in pending if int(entry.get("attempts") or 0) > 0
        ),
        "dead_letter": len(dead_letter),
        "total": len(entries),
        "archived_dead_letter": archived_count,
        "overflow_count": overflow_count,
        "last_overflow_at": last_overflow_at,
        "oldest_created_at": min(
            (float(entry.get("created_at") or 0.0) for entry in entries),
            default=0.0,
        ),
        "next_attempt_at": min(
            (float(entry.get("next_attempt_at") or 0.0) for entry in entries),
            default=0.0,
        ),
    }
