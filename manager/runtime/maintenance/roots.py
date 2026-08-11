"""Admitting manager roots across a maintenance fence.

A root is either admitted now or queued durably for after the window. Both
paths are decided inside the same transaction as the fence itself, so a root
can never be admitted by one process while another raises the fence.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any

from manager.runtime.maintenance.models import (
    ROOT_CLAIM_TTL_SECONDS,
    IngressRootConflictError,
    RootAdmission,
    RootEnqueueResult,
    _integer,
    _number,
)


class RootQueue:
    """The durable queue of manager roots and their claims."""

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

