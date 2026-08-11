"""Roots that arrived while a contact was busy, in the order they arrived.

A queued root holds a lease from the moment it is claimed until the runner
acknowledges it. Losing that lease loses the task, so nothing is dequeued
speculatively.
"""
from __future__ import annotations
from interface.long_tasks import constants
from interface.long_tasks import registry as registry_module
from interface.long_tasks import store as store_module
from interface.long_tasks import util as util_module
import os
import time
from typing import Any
from helpers.state import read_json, update_json, update_json_if_changed


def _queued_root_id(contact_id: str, run_id: str, context: str) -> str:
    return util_module._stable_id("queued-root", contact_id, run_id, context)


def _has_queued_root_backlog(contact_id: str) -> bool:
    state = read_json(constants.LONG_TASK_STATE_FILE, store_module._default_state())
    queued = state.get("queued_roots") if isinstance(state, dict) else {}
    items = queued.get(str(contact_id)) if isinstance(queued, dict) else []
    return bool(isinstance(items, list) and items)


def _is_claimed_queue_head(contact_id: str, root_id: str) -> bool:
    """Return whether *root_id* is this process's leased FIFO queue head.

    A queue marker alone is not authority to cross the backlog fence.  It
    must still name the oldest item and carry the live lease created by
    :func:`claim_ready_long_task_roots`.  That narrow escape edge lets the
    claimed head launch while every later or stale root remains fenced.
    """
    root_id = str(root_id or "")
    if not root_id:
        return False
    state = read_json(constants.LONG_TASK_STATE_FILE, store_module._default_state())
    queued = state.get("queued_roots") if isinstance(state, dict) else {}
    items = queued.get(str(contact_id)) if isinstance(queued, dict) else []
    if (
        not isinstance(items, list)
        or not items
        or not isinstance(items[0], dict)
    ):
        return False
    head = items[0]
    return bool(
        str(head.get("root_id") or "") == root_id
        and str(head.get("claim_owner") or "") == constants._PROCESS_TOKEN
        and int(head.get("claim_pid") or 0) == os.getpid()
        and float(head.get("claim_until") or 0) > time.time()
    )


def queue_long_task_root_if_blocked(
    contact_id: str,
    run_id: str,
    context: str,
    *,
    visible: bool,
    claimed_root_id: str = "",
) -> bool:
    """Durably defer an unrelated root while terminal delivery is fenced."""
    contact_id = str(contact_id)
    run_id = str(run_id or util_module._stable_id("run", contact_id, context))
    lifecycle = registry_module.current_long_task(contact_id)
    if lifecycle is not None and lifecycle.close_if_terminal():
        lifecycle = None
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
        store_module._recover_expired_terminal_entry(contact_id)
        entry = store_module._state_entry(contact_id)
        blocked = bool(
            entry.get("active")
            and run_id != str(entry.get("run_id") or "")
            and (
                entry.get("pending_reply")
                or entry.get("settle_requested")
                or entry.get("terminal")
            )
        )
    # Once a root has crossed the durable queue fence, later roots must stay
    # behind it even if the stale lifecycle was just recovered.  Only the
    # live, process-owned FIFO claim may cross the backlog part of the fence;
    # it never bypasses an active lifecycle's terminal-delivery fence.
    backlog_blocked = _has_queued_root_backlog(contact_id)
    if backlog_blocked and _is_claimed_queue_head(
        contact_id, claimed_root_id
    ):
        backlog_blocked = False
    blocked = blocked or backlog_blocked
    if not blocked:
        return False

    root_id = _queued_root_id(contact_id, run_id, context)
    now = time.time()

    def mutate(state: dict[str, Any]) -> None:
        store_module._prune_state_locked(state, now)
        queued = state.setdefault("queued_roots", {})
        items = queued.setdefault(contact_id, [])
        if any(item.get("root_id") == root_id for item in items):
            return
        # Only the newest periodic accuracy review remains meaningful. Keeping
        # stale reviews while a contact is blocked can fill the durable queue
        # with superseded copies that can never drain.
        if util_module._is_internal_accuracy_review(context):
            items[:] = [
                item
                for item in items
                if not util_module._is_internal_accuracy_review(item.get("context"))
            ]
        total = sum(
            len(value)
            for value in queued.values()
            if isinstance(value, list)
        )
        if (
            len(items) >= constants.MAX_QUEUED_ROOTS_PER_CONTACT
            or total >= constants.MAX_QUEUED_ROOTS
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

    update_json(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)
    return True


def claim_ready_long_task_roots(
    *,
    limit: int = 16,
) -> dict[str, str]:
    """Claim one queued root per idle contact for the local dispatcher."""
    claimed: dict[str, str] = {}
    owner = constants._PROCESS_TOKEN
    now = time.time()
    bounded_limit = max(0, min(int(limit), 64))

    def mutate(state: dict[str, Any]) -> None:
        store_module._prune_state_locked(state, now)
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
            if (
                isinstance(entry, dict)
                and store_module._recoverable_terminal_entry(entry, now)
            ):
                contacts[contact_id] = store_module._tombstone(entry, now)
                entry = contacts[contact_id]
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
                and util_module._pid_alive(item.get("claim_pid"))
            ):
                continue
            item["claim_owner"] = owner
            item["claim_pid"] = os.getpid()
            item["claim_until"] = now + constants.QUEUED_ROOT_LEASE_SECONDS
            claimed[str(contact_id)] = (
                f"{constants._QUEUED_ROOT_MARKER} {item['root_id']}\n"
                f"{constants._QUEUED_ROOT_VISIBILITY_MARKER} "
                f"{1 if item.get('visible', True) else 0}\n"
                f"{str(item.get('context') or '')}"
            )

    update_json_if_changed(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)
    return claimed


def extract_queued_long_task_root_metadata(
    context: str,
) -> tuple[str, str, bool | None]:
    """Extract a queued root and its durable visibility decision."""
    text = str(context or "")
    first, separator, rest = text.partition("\n")
    if not first.startswith(constants._QUEUED_ROOT_MARKER):
        return "", text, None
    root_id = first.removeprefix(constants._QUEUED_ROOT_MARKER).strip()
    clean_context = rest if separator else ""
    visibility: bool | None = None
    visibility_line, visibility_separator, remainder = (
        clean_context.partition("\n")
    )
    if visibility_line.startswith(constants._QUEUED_ROOT_VISIBILITY_MARKER):
        encoded = visibility_line.removeprefix(
            constants._QUEUED_ROOT_VISIBILITY_MARKER
        ).strip()
        if encoded in {"0", "1"}:
            visibility = encoded == "1"
        clean_context = remainder if visibility_separator else ""
    return root_id, clean_context, visibility


def extract_accuracy_review_root(context: str) -> tuple[str, str]:
    """Remove the internal accuracy-review delivery marker."""
    text = str(context or "")
    first, separator, rest = text.partition("\n")
    if not first.startswith(constants._ACCURACY_REVIEW_MARKER):
        return "", text
    return (
        first.removeprefix(constants._ACCURACY_REVIEW_MARKER).strip(),
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

    update_json(constants.LONG_TASK_STATE_FILE, store_module._default_state(), mutate)
