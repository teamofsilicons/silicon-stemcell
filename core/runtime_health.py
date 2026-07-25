"""Application-level readiness heartbeat for supervised Silicon runtimes."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from core.runtime_paths import CODE_ROOT, DATA_ROOT
from core.state_store import file_lock, read_json, write_json

HEALTH_FILE = DATA_ROOT / ".silicon" / "runtime-health.json"
HEARTBEAT_SECONDS = 1.0

_guard = threading.Lock()
_stop: threading.Event | None = None
_thread: threading.Thread | None = None
_ready_at = 0.0


def _phase(provider: Callable[[], str] | None) -> str:
    if provider is None:
        return "available"
    try:
        value = str(provider() or "available")
    except Exception:
        value = "unknown"
    return value[:64]


def publish_runtime_health(
    phase_provider: Callable[[], str] | None = None,
) -> dict:
    """Atomically attest that imports/bootstrap reached the live event loop."""

    global _ready_at
    now = time.time()
    if not _ready_at:
        _ready_at = now
    value = {
        "schema": 1,
        "pid": os.getpid(),
        "code_root": str(CODE_ROOT),
        "ready": True,
        "ready_at": _ready_at,
        "heartbeat_at": now,
        "phase": _phase(phase_provider),
    }
    write_json(HEALTH_FILE, value)
    return value


def start_runtime_health(
    phase_provider: Callable[[], str] | None = None,
    *,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> None:
    """Start one idempotent heartbeat thread after runtime bootstrap."""

    global _stop, _thread
    with _guard:
        if _thread is not None and _thread.is_alive():
            return
        publish_runtime_health(phase_provider)
        _stop = threading.Event()

        def heartbeat() -> None:
            while _stop is not None and not _stop.wait(
                max(0.1, float(heartbeat_seconds))
            ):
                try:
                    publish_runtime_health(phase_provider)
                except Exception:
                    # The missing/stale heartbeat makes the external updater
                    # fail health validation. Runtime work itself keeps going.
                    pass

        _thread = threading.Thread(
            target=heartbeat,
            name="silicon-runtime-health",
            daemon=True,
        )
        _thread.start()


def stop_runtime_health() -> None:
    """Stop the heartbeat and remove only this process's readiness record."""

    global _ready_at, _stop, _thread
    with _guard:
        stop = _stop
        thread = _thread
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        _stop = None
        _thread = None
        _ready_at = 0.0
        with file_lock(HEALTH_FILE):
            current = read_json(HEALTH_FILE, {})
            if (
                isinstance(current, dict)
                and current.get("pid") == os.getpid()
            ):
                HEALTH_FILE.unlink(missing_ok=True)
