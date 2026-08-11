"""Durable task-aware maintenance fencing for transactional updates.

The updater and the running Stemcell coordinate through one small JSON state
machine.  Every decision that can race (admit a root task, acquire a
descendant lease, or raise an update fence) is made while holding the same
cross-process lock.

Only sanitized summaries leave this module.  Queued manager contexts are
durable, but they are never included in status or public maintenance events.
"""
from __future__ import annotations

import argparse
import contextvars
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from core.runtime_paths import DATA_ROOT, validated_data_root
from core.state_store import file_lock, read_json, write_json


PROJECT_ROOT = DATA_ROOT

STATE_VERSION = 1
LEASE_TTL_SECONDS = 90.0
ROOT_CLAIM_TTL_SECONDS = 120.0
NOTICE_CLAIM_TTL_SECONDS = 60.0
MAX_PUBLIC_EVENTS = 200
MAX_DELIVERED_NOTICES = 200
MAX_CONTINUATION_RECEIPTS = 2_000
CONTINUATION_RECEIPT_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_INGRESS_RECEIPTS = 5_000
INGRESS_RECEIPT_TTL_SECONDS = 7 * 24 * 60 * 60
RETRY_BACKOFF_CAP_SECONDS = 300.0
RETRY_BACKOFF_MAX_DOUBLINGS = 6


def _retry_backoff_seconds(delay: float, attempts: int) -> float:
    """Back off failed root admission without allowing retry storms."""
    base = max(0.0, float(delay))
    doublings = min(
        max(0, int(attempts) - 1),
        RETRY_BACKOFF_MAX_DOUBLINGS,
    )
    return min(base * (2 ** doublings), RETRY_BACKOFF_CAP_SECONDS)

ACTIVE_PHASES = {"draining", "updating", "validating", "rolling_back"}
PUBLIC_MESSAGES = {
    "available": "Silicon is available.",
    "draining": (
        "Silicon is finishing its current work before updating. "
        "Your message is safely queued; you don't need to resend it."
    ),
    "updating": "Silicon is updating. Your message is safely queued; you don't need to resend it.",
    "validating": "Silicon is restarting and being checked. Your message is safely queued.",
    "rolling_back": "The update could not be completed. Silicon is resuming its previous version.",
}


_CURRENT_ACTIVITY: contextvars.ContextVar["ActivityToken | None"] = contextvars.ContextVar(
    "silicon_maintenance_activity",
    default=None,
)


class IngressRootConflictError(RuntimeError):
    """Body-free signal that a durable ingress identity was reused."""


def _default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "epoch": 0,
        "phase": "available",
        "maintenance_id": "",
        "requested_at": 0.0,
        "deadline_at": 0.0,
        "updated_at": 0.0,
        "safe_to_stop": False,
        "last_outcome": "",
        "leases": {},
        "root_queue": [],
        "continuation_receipts": {},
        "ingress_receipts": {},
        "notices": [],
        "public_events": [],
        "event_sequence": 0,
        "participants": {},
    }


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ActivityToken:
    lease_id: str
    lineage_id: str
    admitted_epoch: int
    kind: str
    parent_lease_id: str = ""

    def reference(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "lineage_id": self.lineage_id,
            "admitted_epoch": self.admitted_epoch,
            "kind": self.kind,
            "parent_lease_id": self.parent_lease_id,
        }


@dataclass(frozen=True)
class RootAdmission:
    queue_id: str
    claim_token: str
    contact_id: str
    context: str
    activity: ActivityToken


@dataclass(frozen=True)
class RootEnqueueResult:
    admission: RootAdmission | None
    queued_for_maintenance: bool
    maintenance_id: str
    public_state: str


class MaintenanceCoordinator:
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
            or self.root / "core" / "interface_state" / "maintenance.json"
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

    def enqueue_root(self, contact_id: str, context: str) -> RootEnqueueResult:
        """Durably queue a manager root and atomically admit it when available."""
        result: RootEnqueueResult | None = None

        def mutate(state: dict[str, Any], now: float) -> None:
            nonlocal result
            queue_id = uuid.uuid4().hex
            phase = str(state.get("phase") or "available")
            item = {
                "queue_id": queue_id,
                "contact_id": str(contact_id),
                "context": str(context),
                "enqueued_at": now,
                "enqueued_epoch": _integer(state.get("epoch")),
                "status": "pending",
                "claim_token": "",
                "claim_until": 0.0,
                "lease_id": "",
                "attempts": 0,
                "not_before": 0.0,
            }
            state["root_queue"].append(item)
            if phase == "available":
                admission = self._claim_root_item(state, item, now)
                result = RootEnqueueResult(
                    admission=admission,
                    queued_for_maintenance=False,
                    maintenance_id="",
                    public_state="available",
                )
                return

            self._ensure_notice(state, str(contact_id), phase, now)
            result = RootEnqueueResult(
                admission=None,
                queued_for_maintenance=True,
                maintenance_id=str(state.get("maintenance_id") or ""),
                public_state=phase,
            )

        self._transaction(mutate)
        assert result is not None
        return result

    def enqueue_ingress_root(
        self,
        contact_id: str,
        context: str,
        *,
        ingress_id: str,
    ) -> bool:
        """Idempotently take ownership of one durable source record.

        Unlike ``enqueue_root``, an ingress root always remains pending. This
        lets an event-loop handler transfer ownership before acknowledging its
        source without creating a claimed root that has not yet been handed to
        the runtime dispatcher.
        """
        contact_id = str(contact_id)
        context = str(context)
        ingress_id = str(ingress_id or "")
        if not ingress_id:
            raise ValueError("A durable ingress identity is required.")
        queue_id = (
            "ingress-"
            + hashlib.sha256(ingress_id.encode("utf-8")).hexdigest()
        )
        fingerprint = hashlib.sha256(
            f"{contact_id}\x1f{context}".encode("utf-8")
        ).hexdigest()
        accepted = False

        def mutate(state: dict[str, Any], now: float) -> None:
            nonlocal accepted
            receipt = state["ingress_receipts"].get(queue_id)
            if isinstance(receipt, dict):
                if (
                    receipt.get("fingerprint") != fingerprint
                    or receipt.get("contact_id") != contact_id
                ):
                    raise IngressRootConflictError(
                        "Durable ingress identity conflicts with an accepted root."
                    )
                accepted = True
                return

            existing = next(
                (
                    item
                    for item in state["root_queue"]
                    if isinstance(item, dict)
                    and str(item.get("queue_id") or "") == queue_id
                ),
                None,
            )
            if isinstance(existing, dict):
                existing_fingerprint = hashlib.sha256(
                    (
                        f"{str(existing.get('contact_id') or '')}\x1f"
                        f"{str(existing.get('context') or '')}"
                    ).encode("utf-8")
                ).hexdigest()
                if (
                    str(existing.get("contact_id") or "") != contact_id
                    or existing_fingerprint != fingerprint
                ):
                    raise IngressRootConflictError(
                        "Durable ingress identity conflicts with an accepted root."
                    )
                state["ingress_receipts"][queue_id] = {
                    "contact_id": contact_id,
                    "fingerprint": fingerprint,
                    "accepted_at": _number(existing.get("enqueued_at"), now),
                }
                accepted = True
                return

            phase = str(state.get("phase") or "available")
            state["root_queue"].append(
                {
                    "queue_id": queue_id,
                    "contact_id": contact_id,
                    "context": context,
                    "enqueued_at": now,
                    "enqueued_epoch": _integer(state.get("epoch")),
                    "status": "pending",
                    "claim_token": "",
                    "claim_until": 0.0,
                    "lease_id": "",
                    "attempts": 0,
                    "not_before": 0.0,
                }
            )
            state["ingress_receipts"][queue_id] = {
                "contact_id": contact_id,
                "fingerprint": fingerprint,
                "accepted_at": now,
            }
            if phase != "available":
                self._ensure_notice(state, contact_id, phase, now)
            state["safe_to_stop"] = False
            accepted = True

        self._transaction(mutate)
        return accepted

    def _claim_root_item(
        self,
        state: dict[str, Any],
        item: dict[str, Any],
        now: float,
    ) -> RootAdmission:
        claim_token = uuid.uuid4().hex
        continuation = bool(item.get("continuation"))
        transfer_lease_id = str(item.get("transfer_lease_id") or "")
        activity = self._new_lease(
            state,
            now,
            kind="manager_root",
            activity_id=str(item.get("queue_id") or ""),
            contact_id=str(item.get("contact_id") or ""),
            admitted_epoch=(
                _integer(item.get("admitted_epoch"))
                if continuation
                else _integer(state.get("epoch"))
            ),
            lineage_id=(
                str(item.get("lineage_id") or "")
                if continuation
                else ""
            ),
            parent_lease_id=transfer_lease_id,
        )
        if transfer_lease_id:
            state["leases"].pop(transfer_lease_id, None)
        item["status"] = "claimed"
        item["claim_token"] = claim_token
        item["claim_until"] = now + ROOT_CLAIM_TTL_SECONDS
        item["lease_id"] = activity.lease_id
        return RootAdmission(
            queue_id=str(item.get("queue_id") or ""),
            claim_token=claim_token,
            contact_id=str(item.get("contact_id") or ""),
            context=str(item.get("context") or ""),
            activity=activity,
        )

    def claim_pending_roots(self, *, limit: int = 100) -> list[RootAdmission]:
        admissions: list[RootAdmission] = []

        def mutate(state: dict[str, Any], now: float) -> None:
            phase = str(state.get("phase") or "available")
            if phase not in {"available", "draining"}:
                return
            for item in state["root_queue"]:
                if len(admissions) >= max(1, int(limit)):
                    break
                if not isinstance(item, dict) or item.get("status") != "pending":
                    continue
                if _number(item.get("not_before")) > now:
                    continue
                if phase == "draining" and not (
                    item.get("continuation")
                    and _integer(item.get("admitted_epoch"))
                    < _integer(state.get("epoch"))
                ):
                    continue
                admissions.append(self._claim_root_item(state, item, now))

        self._transaction(mutate)
        return admissions

    def enqueue_continuation(
        self,
        contact_id: str,
        context: str,
        activity_reference: dict[str, Any],
        *,
        queue_id: str = "",
    ) -> bool:
        """Idempotently transfer an accepted descendant into a manager turn."""
        transferred = False
        queue_id = str(queue_id or uuid.uuid4().hex)
        contact_id = str(contact_id)
        context = str(context)
        fingerprint = hashlib.sha256(
            f"{contact_id}\x1f{context}".encode("utf-8")
        ).hexdigest()

        def mutate(state: dict[str, Any], now: float) -> None:
            nonlocal transferred
            receipt = state["continuation_receipts"].get(queue_id)
            if isinstance(receipt, dict):
                transferred = (
                    receipt.get("fingerprint") == fingerprint
                    and receipt.get("contact_id") == contact_id
                )
                return
            existing = next(
                (
                    item
                    for item in state["root_queue"]
                    if isinstance(item, dict)
                    and str(item.get("queue_id") or "") == queue_id
                ),
                None,
            )
            if isinstance(existing, dict):
                transferred = (
                    str(existing.get("contact_id") or "") == contact_id
                    and hashlib.sha256(
                        (
                            f"{contact_id}\x1f"
                            f"{str(existing.get('context') or '')}"
                        ).encode("utf-8")
                    ).hexdigest()
                    == fingerprint
                )
                if transferred:
                    state["continuation_receipts"][queue_id] = {
                        "contact_id": contact_id,
                        "fingerprint": fingerprint,
                        "accepted_at": _number(existing.get("enqueued_at"), now),
                    }
                return
            lease_id = str((activity_reference or {}).get("lease_id") or "")
            lease = state["leases"].get(lease_id)
            if not isinstance(lease, dict):
                return
            state["root_queue"].append(
                {
                    "queue_id": queue_id,
                    "contact_id": contact_id,
                    "context": context,
                    "enqueued_at": now,
                    "enqueued_epoch": _integer(state.get("epoch")),
                    "status": "pending",
                    "claim_token": "",
                    "claim_until": 0.0,
                    "lease_id": "",
                    "attempts": 0,
                    "not_before": 0.0,
                    "continuation": True,
                    "admitted_epoch": _integer(lease.get("admitted_epoch")),
                    "lineage_id": str(lease.get("lineage_id") or lease_id),
                    "transfer_lease_id": lease_id,
                }
            )
            # The pending continuation itself is counted as blocking activity,
            # so transferring the process lease creates no quiescence gap.
            state["leases"].pop(lease_id, None)
            state["continuation_receipts"][queue_id] = {
                "contact_id": contact_id,
                "fingerprint": fingerprint,
                "accepted_at": now,
            }
            state["safe_to_stop"] = False
            transferred = True

        self._transaction(mutate)
        return transferred

    def heartbeat(self, token_or_id: ActivityToken | str, *, ttl: float = LEASE_TTL_SECONDS) -> bool:
        lease_id = (
            token_or_id.lease_id
            if isinstance(token_or_id, ActivityToken)
            else str(token_or_id or "")
        )
        alive = False

        def mutate(state: dict[str, Any], now: float) -> None:
            nonlocal alive
            lease = state["leases"].get(lease_id)
            if not isinstance(lease, dict):
                return
            lease["heartbeat_at"] = now
            lease["expires_at"] = now + max(5.0, float(ttl))
            alive = True
            for item in state["root_queue"]:
                if isinstance(item, dict) and item.get("lease_id") == lease_id:
                    item["claim_until"] = now + ROOT_CLAIM_TTL_SECONDS

        self._transaction(mutate)
        return alive

    def get_activity(self, lease_id: str) -> ActivityToken | None:
        token: ActivityToken | None = None

        def mutate(state: dict[str, Any], _now: float) -> None:
            nonlocal token
            lease = state["leases"].get(str(lease_id or ""))
            if isinstance(lease, dict):
                token = self._token_from_lease(str(lease_id), lease)

        self._transaction(mutate)
        return token

    def acquire_activity(
        self,
        kind: str,
        *,
        activity_id: str = "",
        contact_id: str = "",
        parent: ActivityToken | None = None,
        ttl: float = LEASE_TTL_SECONDS,
    ) -> ActivityToken | None:
        """Acquire a descendant lease or reject it behind the active fence."""
        parent = parent if parent is not None else _CURRENT_ACTIVITY.get()
        acquired: ActivityToken | None = None

        def mutate(state: dict[str, Any], now: float) -> None:
            nonlocal acquired
            phase = str(state.get("phase") or "available")
            if phase == "available":
                admitted_epoch = _integer(state.get("epoch"))
                lineage_id = parent.lineage_id if parent else ""
                parent_id = parent.lease_id if parent else ""
            else:
                if parent is None:
                    return
                parent_lease = state["leases"].get(parent.lease_id)
                if not isinstance(parent_lease, dict):
                    return
                # The lineage must have been admitted before this fence.
                if parent.admitted_epoch >= _integer(state.get("epoch")):
                    return
                admitted_epoch = parent.admitted_epoch
                lineage_id = parent.lineage_id
                parent_id = parent.lease_id
            acquired = self._new_lease(
                state,
                now,
                kind=kind,
                activity_id=activity_id,
                contact_id=contact_id,
                admitted_epoch=admitted_epoch,
                lineage_id=lineage_id,
                parent_lease_id=parent_id,
                ttl=ttl,
            )

        self._transaction(mutate)
        return acquired

    def adopt_prefence_activity(
        self,
        kind: str,
        *,
        activity_id: str,
        contact_id: str = "",
        started_at: float = 0.0,
        ttl: float = LEASE_TTL_SECONDS,
    ) -> ActivityToken | None:
        """Lease legacy runtime work that demonstrably predates the fence.

        This closes the upgrade edge where an active worker record was written
        by an older Stemcell and therefore has no maintenance reference yet.
        Work created after the fence is never adopted.
        """
        acquired: ActivityToken | None = None

        def mutate(state: dict[str, Any], now: float) -> None:
            nonlocal acquired
            phase = str(state.get("phase") or "available")
            epoch = _integer(state.get("epoch"))
            if phase == "available":
                admitted_epoch = epoch
            else:
                requested_at = _number(state.get("requested_at"))
                if not requested_at:
                    return
                # Missing timestamps exist in early worker-state formats. They
                # are conservatively treated as pre-fence so an update can
                # never kill unknown in-flight work.
                if started_at and float(started_at) > requested_at:
                    return
                admitted_epoch = max(0, epoch - 1)
            acquired = self._new_lease(
                state,
                now,
                kind=kind,
                activity_id=activity_id,
                contact_id=contact_id,
                admitted_epoch=admitted_epoch,
                ttl=ttl,
            )

        self._transaction(mutate)
        return acquired

    def release(self, token_or_id: ActivityToken | str) -> bool:
        lease_id = (
            token_or_id.lease_id
            if isinstance(token_or_id, ActivityToken)
            else str(token_or_id or "")
        )
        released = False

        def mutate(state: dict[str, Any], _now: float) -> None:
            nonlocal released
            released = state["leases"].pop(lease_id, None) is not None

        self._transaction(mutate)
        return released

    def complete_roots(self, admissions: Sequence[RootAdmission]) -> None:
        keys = {
            item.queue_id: (item.claim_token, item.activity.lease_id)
            for item in admissions
        }

        def mutate(state: dict[str, Any], _now: float) -> None:
            retained = []
            for item in state["root_queue"]:
                key = keys.get(str(item.get("queue_id") or "")) if isinstance(item, dict) else None
                if (
                    key is None
                    or item.get("claim_token") != key[0]
                    or item.get("lease_id") != key[1]
                ):
                    retained.append(item)
                    continue
                state["leases"].pop(key[1], None)
            state["root_queue"] = retained

        self._transaction(mutate)

    def retry_roots(self, admissions: Sequence[RootAdmission], *, delay: float = 5.0) -> None:
        keys = {
            item.queue_id: (item.claim_token, item.activity.lease_id)
            for item in admissions
        }

        def mutate(state: dict[str, Any], now: float) -> None:
            for item in state["root_queue"]:
                if not isinstance(item, dict):
                    continue
                key = keys.get(str(item.get("queue_id") or ""))
                if (
                    key is None
                    or item.get("claim_token") != key[0]
                    or item.get("lease_id") != key[1]
                ):
                    continue
                state["leases"].pop(key[1], None)
                item["status"] = "pending"
                item["claim_token"] = ""
                item["claim_until"] = 0.0
                item["lease_id"] = ""
                attempts = _integer(item.get("attempts")) + 1
                item["attempts"] = attempts
                item["not_before"] = now + _retry_backoff_seconds(
                    delay,
                    attempts,
                )

        self._transaction(mutate)

    def _ensure_notice(
        self,
        state: dict[str, Any],
        contact_id: str,
        phase: str,
        now: float,
    ) -> None:
        maintenance_id = str(state.get("maintenance_id") or "")
        if any(
            isinstance(item, dict)
            and item.get("maintenance_id") == maintenance_id
            and item.get("contact_id") == contact_id
            for item in state["notices"]
        ):
            return
        state["notices"].append(
            {
                "notice_id": uuid.uuid4().hex,
                "maintenance_id": maintenance_id,
                "contact_id": contact_id,
                "phase": phase,
                "message": PUBLIC_MESSAGES.get(phase, PUBLIC_MESSAGES["updating"]),
                "created_at": now,
                "status": "pending",
                "claim_token": "",
                "claim_until": 0.0,
                "delivered_at": 0.0,
            }
        )

    def claim_notices(self, *, limit: int = 20) -> list[dict[str, str]]:
        claimed: list[dict[str, str]] = []

        def mutate(state: dict[str, Any], now: float) -> None:
            for item in state["notices"]:
                if len(claimed) >= max(1, int(limit)):
                    break
                if not isinstance(item, dict) or item.get("status") != "pending":
                    continue
                token = uuid.uuid4().hex
                item["status"] = "claimed"
                item["claim_token"] = token
                item["claim_until"] = now + NOTICE_CLAIM_TTL_SECONDS
                claimed.append(
                    {
                        "notice_id": str(item.get("notice_id") or ""),
                        "claim_token": token,
                        "contact_id": str(item.get("contact_id") or ""),
                        "maintenance_id": str(item.get("maintenance_id") or ""),
                        "phase": str(item.get("phase") or ""),
                        "message": str(item.get("message") or ""),
                    }
                )

        self._transaction(mutate)
        return claimed

    def finish_notice(self, notice_id: str, claim_token: str, *, delivered: bool) -> bool:
        finished = False

        def mutate(state: dict[str, Any], now: float) -> None:
            nonlocal finished
            for item in state["notices"]:
                if (
                    not isinstance(item, dict)
                    or item.get("notice_id") != notice_id
                    or item.get("claim_token") != claim_token
                ):
                    continue
                if delivered:
                    item["status"] = "delivered"
                    item["delivered_at"] = now
                else:
                    item["status"] = "pending"
                item["claim_token"] = ""
                item["claim_until"] = 0.0
                finished = True
                break

        self._transaction(mutate)
        return finished

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


COORDINATOR = MaintenanceCoordinator()


def current_activity() -> ActivityToken | None:
    return _CURRENT_ACTIVITY.get()


@contextmanager
def bind_activity(activity: ActivityToken | None) -> Iterator[None]:
    token = _CURRENT_ACTIVITY.set(activity)
    try:
        yield
    finally:
        _CURRENT_ACTIVITY.reset(token)


@contextmanager
def heartbeat_scope(
    activities: Sequence[ActivityToken],
    *,
    coordinator: MaintenanceCoordinator | None = None,
) -> Iterator[None]:
    """Bind a lineage and keep every supplied lease alive until scope exit."""
    coordinator = coordinator or COORDINATOR
    active = [item for item in activities if item is not None]
    stop = threading.Event()

    def beat() -> None:
        interval = max(1.0, LEASE_TTL_SECONDS / 3.0)
        while not stop.wait(interval):
            for item in active:
                coordinator.heartbeat(item)

    thread = None
    if active:
        thread = threading.Thread(
            target=beat,
            name="maintenance-lease-heartbeat",
            daemon=True,
        )
        thread.start()
    with bind_activity(active[0] if active else None):
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=1)


def public_status(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    coordinator = COORDINATOR if root is None else MaintenanceCoordinator(root)
    return coordinator.public_status()


def accepting_new_roots() -> bool:
    return COORDINATOR.public_status()["phase"] == "available"


def acquire_descendant_activity(
    kind: str,
    *,
    activity_id: str = "",
    contact_id: str = "",
) -> ActivityToken | None:
    return COORDINATOR.acquire_activity(
        kind,
        activity_id=activity_id,
        contact_id=contact_id,
    )


def heartbeat_activity(token_or_id: ActivityToken | str) -> bool:
    return COORDINATOR.heartbeat(token_or_id)


def release_activity(token_or_id: ActivityToken | str) -> bool:
    return COORDINATOR.release(token_or_id)


def activity_from_reference(reference: Any) -> ActivityToken | None:
    if not isinstance(reference, dict):
        return None
    lease_id = str(reference.get("lease_id") or "")
    return COORDINATOR.get_activity(lease_id) if lease_id else None


def _json_print(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    """Machine-readable maintenance hook used by silicon-cli."""
    parser = argparse.ArgumentParser(prog="silicon-maintenance")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)

    request_parser = subparsers.add_parser("request")
    request_parser.add_argument("--deadline", type=float)
    request_parser.add_argument("--id", default="")

    subparsers.add_parser("status")

    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("--id", default="")

    phase_parser = subparsers.add_parser("phase")
    phase_parser.add_argument(
        "phase",
        choices=["updating", "validating", "rolling_back", "available"],
    )
    phase_parser.add_argument("--id", default="")

    wait_parser = subparsers.add_parser("wait")
    wait_parser.add_argument("--timeout", type=float, default=0.0)
    wait_parser.add_argument("--poll", type=float, default=0.2)

    events_parser = subparsers.add_parser("events")
    events_parser.add_argument("--after", type=int, default=0)

    args = parser.parse_args(list(argv) if argv is not None else None)
    coordinator = MaintenanceCoordinator(args.root)
    try:
        if args.command == "request":
            _json_print(
                coordinator.request_drain(
                    deadline_seconds=args.deadline,
                    maintenance_id=args.id,
                )
            )
            return 0
        if args.command == "status":
            _json_print(coordinator.public_status())
            return 0
        if args.command == "cancel":
            cancelled = coordinator.cancel_drain(args.id)
            _json_print(
                {
                    "cancelled": cancelled,
                    "status": coordinator.public_status(),
                }
            )
            return 0 if cancelled else 2
        if args.command == "phase":
            _json_print(coordinator.transition(args.phase, args.id))
            return 0
        if args.command == "events":
            _json_print({"events": coordinator.public_events(after_sequence=args.after)})
            return 0
        if args.command == "wait":
            deadline = time.monotonic() + max(0.0, args.timeout)
            while True:
                status = coordinator.public_status()
                if status["safe_to_stop"]:
                    _json_print(status)
                    return 0
                if args.timeout <= 0 or time.monotonic() >= deadline:
                    _json_print(status)
                    return 2
                time.sleep(max(0.05, min(float(args.poll), 5.0)))
    except (RuntimeError, ValueError) as exc:
        _json_print({"error": str(exc), "status": coordinator.public_status()})
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
