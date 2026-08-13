"""The vocabulary of a maintenance window.

A lease is the right to be doing something; an admission is the right for one
manager root to run. Both are values, both are handed across process
boundaries as plain references, and both are checked rather than trusted when
they come back.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

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


