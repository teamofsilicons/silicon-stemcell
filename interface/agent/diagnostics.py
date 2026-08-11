"""Shipping the diagnosis store to Glass, a bounded batch at a time."""
from __future__ import annotations

import os
import time
from pathlib import Path

from interface.agent.config import detect_status
from interface.agent.frames import send_json

DIAGNOSTICS_INTERVAL = 60
DIAGNOSTIC_RECOVERY_CHECK_INTERVAL = 60
DIAGNOSTIC_RECOVERY_CHECKS: dict[tuple[str, str, int | None], float] = {}


def drain_diagnostics(ws, root: Path, config: dict) -> int:
    """Push completed traces over the authenticated agent socket, fail-open."""
    if config.get("diag_push", True) is False:
        return 0
    coordinator = None
    activity = None
    heartbeat_context = None
    try:
        from manager.runtime.maintenance import MaintenanceCoordinator, heartbeat_scope

        coordinator = MaintenanceCoordinator(root)
        activity = coordinator.acquire_activity(
            "glass_diagnostics",
            activity_id="diagnostic-drain",
        )
        if activity is None:
            return 0
        heartbeat_context = heartbeat_scope(
            [activity],
            coordinator=coordinator,
        )
        heartbeat_context.__enter__()
        from diagnostics.push import (
            drain,
            recover_abandoned_traces,
            resolve_db_path,
        )

        db_path = resolve_db_path(root, config.get("diag_db"))
        service_status = detect_status(root)
        current_pid = None
        try:
            current_pid = int((root / ".silicon.pid").read_text(encoding="utf-8").strip())
        except (OSError, TypeError, ValueError):
            pass
        recovery_signature = (str(root), service_status, current_pid)
        last_recovery_check = DIAGNOSTIC_RECOVERY_CHECKS.get(recovery_signature, 0)
        if time.time() - last_recovery_check >= DIAGNOSTIC_RECOVERY_CHECK_INTERVAL:
            recovered = recover_abandoned_traces(
                db_path,
                current_pid=current_pid,
                service_running=service_status == "running",
            )
            DIAGNOSTIC_RECOVERY_CHECKS[recovery_signature] = time.time()
            if recovered:
                send_json(ws, {
                    "type": "log",
                    "level": "error",
                    "source": "diagnostics",
                    "msg": f"Recovered {recovered} diagnostic run(s) abandoned by an earlier process.",
                })
        if not os.path.exists(db_path):
            return 0
        return drain(
            db_path,
            lambda frame: send_json(ws, frame),
            mark_on_send=False,
        )
    except Exception as exc:
        print(f"[glass-agent] diagnostics drain deferred: {exc}", flush=True)
        return 0
    finally:
        if heartbeat_context is not None:
            heartbeat_context.__exit__(None, None, None)
        if coordinator is not None and activity is not None:
            coordinator.release(activity)


