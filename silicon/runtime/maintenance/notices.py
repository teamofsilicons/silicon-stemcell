"""Telling contacts a maintenance window happened.

A notice is claimed by exactly one deliverer, and only marked delivered once
the contact has it. An abandoned claim expires and the notice is offered again
rather than being silently dropped.
"""
from __future__ import annotations

import uuid
from typing import Any, Sequence

from silicon.runtime.maintenance.models import (
    NOTICE_CLAIM_TTL_SECONDS,
    PUBLIC_MESSAGES,
    RootAdmission,
    _integer,
    _retry_backoff_seconds,
)


class MaintenanceNotices:
    """The durable queue of notices owed to contacts."""

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

