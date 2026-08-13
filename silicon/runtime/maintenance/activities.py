"""Leases: the right to be doing something, and proof it is still alive.

An activity holds a lease with a TTL. A descendant inherits its parent's
lineage so a whole tree of work can be fenced or drained as one thing, and an
expired lease is reclaimed rather than trusted.
"""
from __future__ import annotations

from typing import Any

from silicon.runtime.maintenance.models import (
    LEASE_TTL_SECONDS,
    ROOT_CLAIM_TTL_SECONDS,
    ActivityToken,
    _CURRENT_ACTIVITY,
    _integer,
    _number,
)


class ActivityLeases:
    """Acquiring, heartbeating, adopting, and releasing activity leases."""

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

