"""Durable task-aware maintenance fencing for transactional updates.

The updater and the running Stemcell coordinate through one small JSON state
machine. Every decision that can race — admit a root task, acquire a
descendant lease, or raise an update fence — is made while holding the same
cross-process lock.

Only sanitized summaries leave this package. Queued manager contexts are
durable, but they are never included in status or public maintenance events.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from silicon.runtime.maintenance.coordinator import MaintenanceCoordinator
from silicon.runtime.maintenance.models import (
    ACTIVE_PHASES,
    LEASE_TTL_SECONDS,
    PUBLIC_MESSAGES,
    STATE_VERSION,
    ActivityToken,
    IngressRootConflictError,
    RootAdmission,
    RootEnqueueResult,
    _CURRENT_ACTIVITY,
)
from silicon.runtime.maintenance.store import PROJECT_ROOT

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


__all__ = [
    "ACTIVE_PHASES",
    "COORDINATOR",
    "LEASE_TTL_SECONDS",
    "PROJECT_ROOT",
    "PUBLIC_MESSAGES",
    "STATE_VERSION",
    "ActivityToken",
    "IngressRootConflictError",
    "MaintenanceCoordinator",
    "RootAdmission",
    "RootEnqueueResult",
    "accepting_new_roots",
    "acquire_descendant_activity",
    "activity_from_reference",
    "bind_activity",
    "current_activity",
    "heartbeat_activity",
    "heartbeat_scope",
    "main",
    "public_status",
    "release_activity",
]
