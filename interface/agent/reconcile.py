"""Reconciling team context and trust policy on the sidecar's schedule.

Both run on their own thread with the same shape: a periodic tick, plus an
immediate run when Glass says something changed. Both coalesce — a burst of
change notifications produces one reconcile, not one per notification.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

TEAM_CONTEXT_INTERVAL = 60
TRUST_POLICY_INTERVAL = 60


class TeamContextReconciler:
    """Run context reconciliation off the WebSocket thread and coalesce nudges."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._condition = threading.Condition()
        self._pending = False
        self._force = False
        self._reasons: set[str] = set()
        self._stopped = False
        self._thread: threading.Thread | None = None

    def request(self, *, force: bool = False, reason: str = "") -> None:
        with self._condition:
            if self._stopped:
                return
            self._pending = True
            self._force = self._force or force
            if reason and len(self._reasons) < 8:
                self._reasons.add(str(reason)[:120])
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="team-context-reconciler",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify()

    def stop(self, timeout: float = 2) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
                force = self._force
                reason = ",".join(sorted(self._reasons))[:240]
                self._pending = False
                self._force = False
                self._reasons.clear()

            try:
                # Dynamic import keeps the sidecar alive across partial updates
                # and makes a missing/transient sync dependency fail open.
                from interface.team_context import reconcile_team_context
                from manager.runtime.maintenance import (
                    MaintenanceCoordinator,
                    heartbeat_scope,
                )

                coordinator = MaintenanceCoordinator(self.root)
                activity = coordinator.acquire_activity(
                    "glass_team_context_sync",
                    activity_id="glass-team-context",
                )
                if activity is None:
                    continue
                try:
                    with heartbeat_scope(
                        [activity],
                        coordinator=coordinator,
                    ):
                        reconcile_team_context(
                            self.root,
                            force=force,
                            reason=reason,
                        )
                finally:
                    coordinator.release(activity)
            except Exception as exc:
                print(
                    f"[glass-agent] team context reconciliation deferred: {str(exc)[:300]}",
                    flush=True,
                )


class TrustPolicyReconciler:
    """Coalesce Glass trust invalidations away from the WebSocket thread."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._condition = threading.Condition()
        self._pending = False
        self._force = False
        self._reasons: set[str] = set()
        self._stopped = False
        self._thread: threading.Thread | None = None

    def request(self, *, force: bool = False, reason: str = "") -> None:
        with self._condition:
            if self._stopped:
                return
            self._pending = True
            self._force = self._force or force
            if reason and len(self._reasons) < 8:
                self._reasons.add(str(reason)[:120])
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="trust-policy-reconciler",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify()

    def stop(self, timeout: float = 2) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
                force = self._force
                reason = ",".join(sorted(self._reasons))[:240]
                self._pending = False
                self._force = False
                self._reasons.clear()
            try:
                from manager.runtime.maintenance import MaintenanceCoordinator, heartbeat_scope
                from interface.trust import reconcile_trust_policy

                coordinator = MaintenanceCoordinator(self.root)
                activity = coordinator.acquire_activity(
                    "glass_trust_sync",
                    activity_id="glass-trust",
                )
                if activity is None:
                    continue
                try:
                    with heartbeat_scope([activity], coordinator=coordinator):
                        reconcile_trust_policy(
                            self.root,
                            force=force,
                            reason=reason,
                        )
                finally:
                    coordinator.release(activity)
            except Exception as exc:
                print(
                    f"[glass-agent] trust reconciliation deferred: {str(exc)[:300]}",
                    flush=True,
                )


def _team_context_change_reason(msg: dict) -> str:
    kind = re.sub(r"[^a-z0-9_.-]+", "-", str(msg.get("kind") or "").lower()).strip("-")
    return f"websocket-invalidation:{kind}" if kind else "websocket-invalidation"


def _request_team_context_reconcile(
    reconciler: TeamContextReconciler | None,
    *,
    force: bool = False,
    reason: str,
) -> None:
    if reconciler is None:
        return
    try:
        reconciler.request(force=force, reason=reason)
    except Exception as exc:
        print(
            f"[glass-agent] team context scheduling deferred: {str(exc)[:300]}",
            flush=True,
        )


def _request_trust_reconcile(
    reconciler: TrustPolicyReconciler | None,
    *,
    force: bool = False,
    reason: str,
) -> None:
    if reconciler is None:
        return
    try:
        reconciler.request(force=force, reason=reason)
    except Exception as exc:
        print(
            f"[glass-agent] trust scheduling deferred: {str(exc)[:300]}",
            flush=True,
        )
