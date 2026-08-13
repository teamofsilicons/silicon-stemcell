"""The one document every maintenance decision is made against.

Every mutation runs inside the same cross-process lock, reads the document,
changes it, and writes it back atomically. Nothing here decides policy — it
only guarantees that two processes never decide at the same time.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from helpers.paths import DATA_ROOT, validated_data_root
from helpers.state import file_lock, read_json, write_json
from silicon.runtime.maintenance.models import (
    ACTIVE_PHASES,
    CONTINUATION_RECEIPT_TTL_SECONDS,
    INGRESS_RECEIPT_TTL_SECONDS,
    LEASE_TTL_SECONDS,
    MAX_CONTINUATION_RECEIPTS,
    MAX_DELIVERED_NOTICES,
    MAX_INGRESS_RECEIPTS,
    MAX_PUBLIC_EVENTS,
    STATE_VERSION,
    ActivityToken,
    _default_state,
    _integer,
    _number,
)

PROJECT_ROOT = DATA_ROOT


class MaintenanceStore:
    """Cross-process coordinator backed by one atomically replaced document."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        state_file: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.root = (
            validated_data_root(root)
            if root is not None
            else PROJECT_ROOT
        )
        self.state_file = Path(
            state_file
            or self.root / "interface" / "state" / "maintenance.json"
        )
        self._clock = clock or time.time

    def _now(self) -> float:
        return float(self._clock())

    def _normalize(self, raw: Any) -> dict[str, Any]:
        state = raw if isinstance(raw, dict) else _default_state()
        defaults = _default_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        if state.get("phase") not in {"available", *ACTIVE_PHASES}:
            state["phase"] = "available"
        if not isinstance(state.get("leases"), dict):
            state["leases"] = {}
        if not isinstance(state.get("continuation_receipts"), dict):
            state["continuation_receipts"] = {}
        if not isinstance(state.get("ingress_receipts"), dict):
            state["ingress_receipts"] = {}
        for key in ("root_queue", "notices", "public_events"):
            if not isinstance(state.get(key), list):
                state[key] = []
        if not isinstance(state.get("participants"), dict):
            state["participants"] = {}
        state["version"] = STATE_VERSION
        state["epoch"] = max(0, _integer(state.get("epoch")))
        state["event_sequence"] = max(0, _integer(state.get("event_sequence")))
        return state

    def _emit(
        self,
        state: dict[str, Any],
        event: str,
        *,
        phase: str | None = None,
        outcome: str = "",
    ) -> None:
        state["event_sequence"] = _integer(state.get("event_sequence")) + 1
        item = {
            "sequence": state["event_sequence"],
            "event": str(event),
            "epoch": _integer(state.get("epoch")),
            "maintenance_id": str(state.get("maintenance_id") or ""),
            "phase": str(phase or state.get("phase") or "available"),
            "occurred_at": self._now(),
        }
        if outcome:
            item["outcome"] = str(outcome)
        state["public_events"].append(item)
        del state["public_events"][:-MAX_PUBLIC_EVENTS]

    def _cancel_expired_drain(self, state: dict[str, Any], now: float) -> None:
        deadline = _number(state.get("deadline_at"))
        if (
            state.get("phase") == "draining"
            and deadline > 0
            and now >= deadline
        ):
            state["phase"] = "available"
            state["safe_to_stop"] = False
            state["last_outcome"] = "deadline_expired"
            state["participants"] = {}
            self._emit(
                state,
                "maintenance.cancelled",
                phase="available",
                outcome="deadline_expired",
            )

    def _prune(self, state: dict[str, Any], now: float) -> None:
        self._cancel_expired_drain(state, now)

        leases = state["leases"]
        expired_ids = {
            lease_id
            for lease_id, lease in list(leases.items())
            if not isinstance(lease, dict)
            or _number(lease.get("expires_at")) <= now
        }
        for lease_id in expired_ids:
            leases.pop(lease_id, None)
        if expired_ids:
            self._emit(state, "activity.expired")

        receipts = state["continuation_receipts"]
        for queue_id, receipt in list(receipts.items()):
            if (
                not isinstance(receipt, dict)
                or now - _number(receipt.get("accepted_at"))
                >= CONTINUATION_RECEIPT_TTL_SECONDS
            ):
                receipts.pop(queue_id, None)
        if len(receipts) > MAX_CONTINUATION_RECEIPTS:
            ordered = sorted(
                receipts,
                key=lambda queue_id: _number(
                    (receipts.get(queue_id) or {}).get("accepted_at")
                ),
            )
            for queue_id in ordered[
                : len(receipts) - MAX_CONTINUATION_RECEIPTS
            ]:
                receipts.pop(queue_id, None)

        ingress_receipts = state["ingress_receipts"]
        for queue_id, receipt in list(ingress_receipts.items()):
            if (
                not isinstance(receipt, dict)
                or now - _number(receipt.get("accepted_at"))
                >= INGRESS_RECEIPT_TTL_SECONDS
            ):
                ingress_receipts.pop(queue_id, None)
        if len(ingress_receipts) > MAX_INGRESS_RECEIPTS:
            ordered = sorted(
                ingress_receipts,
                key=lambda queue_id: _number(
                    (ingress_receipts.get(queue_id) or {}).get("accepted_at")
                ),
            )
            for queue_id in ordered[
                : len(ingress_receipts) - MAX_INGRESS_RECEIPTS
            ]:
                ingress_receipts.pop(queue_id, None)

        for item in state["root_queue"]:
            if not isinstance(item, dict) or item.get("status") != "claimed":
                continue
            lease_id = str(item.get("lease_id") or "")
            claim_until = _number(item.get("claim_until"))
            if lease_id in leases and claim_until > now:
                continue
            item["status"] = "pending"
            item["claim_token"] = ""
            item["claim_until"] = 0.0
            item["lease_id"] = ""

        for notice in state["notices"]:
            if not isinstance(notice, dict) or notice.get("status") != "claimed":
                continue
            if _number(notice.get("claim_until")) <= now:
                notice["status"] = "pending"
                notice["claim_token"] = ""
                notice["claim_until"] = 0.0

        delivered = [
            notice
            for notice in state["notices"]
            if isinstance(notice, dict) and notice.get("status") == "delivered"
        ]
        if len(delivered) > MAX_DELIVERED_NOTICES:
            remove_ids = {
                str(item.get("notice_id") or "")
                for item in delivered[:-MAX_DELIVERED_NOTICES]
            }
            state["notices"] = [
                item
                for item in state["notices"]
                if str(item.get("notice_id") or "") not in remove_ids
            ]

        if state.get("phase") != "draining":
            state["safe_to_stop"] = False
        elif state.get("safe_to_stop") and leases:
            # A pre-fence descendant won the race before activation.  Revoke
            # the acknowledgement; the updater must poll the authoritative
            # state again before stopping the process.
            state["safe_to_stop"] = False

    def _transaction(self, mutate: Callable[[dict[str, Any], float], Any]) -> Any:
        with file_lock(self.state_file):
            raw_state = read_json(self.state_file, _default_state())
            previous = deepcopy(raw_state)
            state = self._normalize(raw_state)
            now = self._now()
            self._prune(state, now)
            result = mutate(state, now)
            if state != previous:
                state["updated_at"] = now
                write_json(self.state_file, state)
            return result

    def _read(self) -> dict[str, Any]:
        return self._transaction(lambda state, _now: json.loads(json.dumps(state)))

    @staticmethod
    def _token_from_lease(lease_id: str, lease: dict[str, Any]) -> ActivityToken:
        return ActivityToken(
            lease_id=lease_id,
            lineage_id=str(lease.get("lineage_id") or lease_id),
            admitted_epoch=_integer(lease.get("admitted_epoch")),
            kind=str(lease.get("kind") or "activity"),
            parent_lease_id=str(lease.get("parent_lease_id") or ""),
        )

    def _new_lease(
        self,
        state: dict[str, Any],
        now: float,
        *,
        kind: str,
        activity_id: str,
        contact_id: str,
        admitted_epoch: int,
        lineage_id: str = "",
        parent_lease_id: str = "",
        ttl: float = LEASE_TTL_SECONDS,
    ) -> ActivityToken:
        lease_id = uuid.uuid4().hex
        lineage_id = lineage_id or lease_id
        lease = {
            "kind": str(kind or "activity")[:64],
            # These identifiers are local-only and never included in public
            # status.  Do not store task bodies or manager context here.
            "activity_id": str(activity_id or "")[:200],
            "contact_id": str(contact_id or "")[:200],
            "lineage_id": lineage_id,
            "parent_lease_id": str(parent_lease_id or ""),
            "admitted_epoch": int(admitted_epoch),
            "owner_pid": os.getpid(),
            "acquired_at": now,
            "heartbeat_at": now,
            "expires_at": now + max(5.0, float(ttl)),
        }
        state["leases"][lease_id] = lease
        state["safe_to_stop"] = False
        return self._token_from_lease(lease_id, lease)

