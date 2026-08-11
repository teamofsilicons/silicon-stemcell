"""Raising and lowering the update fence.

A drain asks every manager to reach a safe boundary; a transition moves the
window through its phases. Both are announced as sanitized public events —
queued manager contexts are durable but never leave this module.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from manager.runtime.maintenance.models import (
    ACTIVE_PHASES,
    PUBLIC_MESSAGES,
    STATE_VERSION,
    _integer,
    _number,
)


class DrainControl:
    """Requesting, cancelling, and advancing a maintenance window."""

    def request_drain(
        self,
        *,
        deadline_seconds: float | None = None,
        maintenance_id: str = "",
    ) -> dict[str, Any]:
        """Raise a monotonic fence. Existing lineages may continue."""

        def mutate(state: dict[str, Any], now: float) -> None:
            if state.get("phase") in ACTIVE_PHASES:
                active_id = str(state.get("maintenance_id") or "")
                if maintenance_id and maintenance_id != active_id:
                    raise RuntimeError(
                        "A different Silicon maintenance operation is already active."
                    )
                return
            state["epoch"] = _integer(state.get("epoch")) + 1
            state["phase"] = "draining"
            state["maintenance_id"] = maintenance_id or f"update-{uuid.uuid4().hex}"
            state["requested_at"] = now
            state["deadline_at"] = (
                now + max(0.0, float(deadline_seconds))
                if deadline_seconds is not None
                else 0.0
            )
            state["safe_to_stop"] = False
            state["last_outcome"] = ""
            state["participants"] = {}
            self._emit(state, "maintenance.requested")

        self._transaction(mutate)
        return self.public_status()

    def cancel_drain(self, maintenance_id: str = "", *, outcome: str = "cancelled") -> bool:
        cancelled = False

        def mutate(state: dict[str, Any], _now: float) -> None:
            nonlocal cancelled
            if state.get("phase") != "draining":
                return
            if maintenance_id and maintenance_id != state.get("maintenance_id"):
                return
            state["phase"] = "available"
            state["safe_to_stop"] = False
            state["last_outcome"] = outcome
            state["participants"] = {}
            self._emit(state, "maintenance.cancelled", outcome=outcome)
            cancelled = True

        self._transaction(mutate)
        return cancelled

    def transition(self, phase: str, maintenance_id: str = "") -> dict[str, Any]:
        """Advance the updater-visible lifecycle.

        ``updating`` is accepted only after the runtime has durably declared
        quiescence for the current epoch.
        """
        phase = str(phase or "").strip().lower().replace("-", "_")
        if phase == "rollback":
            phase = "rolling_back"
        if phase not in {"updating", "validating", "rolling_back", "available"}:
            raise ValueError(f"invalid maintenance phase: {phase}")

        def mutate(state: dict[str, Any], _now: float) -> None:
            if maintenance_id and maintenance_id != state.get("maintenance_id"):
                raise RuntimeError("maintenance id does not match the active update")
            current = str(state.get("phase") or "available")
            allowed = {
                # A drain that is abandoned must be able to unwind. An update
                # interrupted before the stop boundary never leaves "draining",
                # and the recovery path asks for "rolling_back" regardless. With
                # no such edge the request was refused, the transaction stayed
                # interrupted, and every later update failed its preflight with
                # "cannot preflight over an interrupted update" until someone
                # walked the state machine by hand.
                "draining": {"updating", "available", "rolling_back"},
                "updating": {"validating", "rolling_back"},
                "validating": {"available", "rolling_back"},
                "rolling_back": {"available"},
                "available": set(),
            }
            if phase == current:
                return
            if phase not in allowed.get(current, set()):
                raise RuntimeError(f"invalid maintenance transition: {current} -> {phase}")
            if phase == "updating" and not state.get("safe_to_stop"):
                raise RuntimeError("runtime has not reached safe_to_stop")
            state["phase"] = phase
            state["safe_to_stop"] = False
            if phase == "available":
                # Report what actually happened. Only a run that reached
                # validation actually updated anything; going straight from
                # "draining" means the update was abandoned before it touched
                # the instance, and recording that as "updated" misreports a
                # Silicon still on its old version as freshly upgraded.
                state["last_outcome"] = {
                    "rolling_back": "rolled_back",
                    "draining": "cancelled",
                }.get(current, "updated")
                state["participants"] = {}
                self._emit(
                    state,
                    "maintenance.available",
                    outcome=str(state.get("last_outcome") or ""),
                )
            else:
                self._emit(state, f"maintenance.{phase}")

        self._transaction(mutate)
        return self.public_status()


    def acknowledge_runtime_quiescent(
        self,
        *,
        epoch: int,
        outbox_flushed: bool,
        pid: int | None = None,
    ) -> bool:
        acknowledged = False

        def mutate(state: dict[str, Any], now: float) -> None:
            nonlocal acknowledged
            if (
                state.get("phase") != "draining"
                or _integer(state.get("epoch")) != int(epoch)
                or state["leases"]
                or any(
                    isinstance(item, dict)
                    and item.get("status") == "pending"
                    and item.get("continuation")
                    for item in state["root_queue"]
                )
                or not outbox_flushed
            ):
                state["safe_to_stop"] = False
                return
            state["participants"]["runtime"] = {
                "epoch": int(epoch),
                "pid": int(pid or os.getpid()),
                "acknowledged_at": now,
            }
            state["safe_to_stop"] = True
            acknowledged = True

        self._transaction(mutate)
        return acknowledged

    def public_events(self, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        state = self._read()
        return [
            dict(item)
            for item in state["public_events"]
            if isinstance(item, dict)
            and _integer(item.get("sequence")) > int(after_sequence)
        ]

    def public_status(self) -> dict[str, Any]:
        state = self._read()
        counts: dict[str, int] = {}
        for lease in state["leases"].values():
            if not isinstance(lease, dict):
                continue
            kind = str(lease.get("kind") or "activity")
            counts[kind] = counts.get(kind, 0) + 1
        continuation_count = sum(
            1
            for item in state["root_queue"]
            if isinstance(item, dict)
            and item.get("status") == "pending"
            and item.get("continuation")
        )
        if continuation_count:
            counts["continuation_pending"] = continuation_count
        phase = str(state.get("phase") or "available")
        pending_roots = sum(
            1
            for item in state["root_queue"]
            if isinstance(item, dict) and item.get("status") == "pending"
        )
        pending_notices = sum(
            1
            for item in state["notices"]
            if isinstance(item, dict) and item.get("status") != "delivered"
        )
        return {
            "version": STATE_VERSION,
            "epoch": _integer(state.get("epoch")),
            "phase": phase,
            "maintenance_id": str(state.get("maintenance_id") or ""),
            "requested_at": _number(state.get("requested_at")),
            "deadline_at": _number(state.get("deadline_at")),
            "safe_to_stop": bool(state.get("safe_to_stop")),
            "active_count": sum(counts.values()),
            "active_by_kind": counts,
            "queued_message_count": pending_roots,
            "pending_notice_count": pending_notices,
            "public_message": PUBLIC_MESSAGES.get(phase, PUBLIC_MESSAGES["available"]),
            "last_outcome": str(state.get("last_outcome") or ""),
            "event_sequence": _integer(state.get("event_sequence")),
        }


