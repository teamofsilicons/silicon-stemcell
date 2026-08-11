#!/usr/bin/env python3
"""The live connection to Glass control.

Holds one websocket open, reports status, ships diagnostics, answers commands,
and reconnects with backoff when it drops. Everything it does on the way is in
a sibling module; this file is the loop.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from interface.agent.commands import status_payload
from interface.agent.config import (
    AUTH_REJECTION_BACKOFF,
    api_key_from_config,
    is_authentication_rejection,
    load_config,
    local_version,
    prepend_local_bin,
    reconnect_delay,
    silicon_dir,
    silicon_name,
    ssl_context,
    wait_for_retry,
    ws_url,
)
from interface.agent.diagnostics import DIAGNOSTICS_INTERVAL, drain_diagnostics
from interface.agent.frames import send_json
from interface.agent.messages import handle_message
from interface.agent.reconcile import (
    TEAM_CONTEXT_INTERVAL,
    TRUST_POLICY_INTERVAL,
    TeamContextReconciler,
    TrustPolicyReconciler,
    _request_team_context_reconcile,
    _request_trust_reconcile,
)
from interface.agent.tailer import ChangeDrivenWorker, RuntimeLogTailer
from interface.agent.terminal import terminal_stop

STATUS_CHECK_INTERVAL = 5
STATUS_REFRESH_INTERVAL = 60
PING_INTERVAL = 20


def run_live(
    root: Path,
    config: dict,
    running: list[bool],
    *,
    team_context_reconciler: TeamContextReconciler | None = None,
    trust_policy_reconciler: TrustPolicyReconciler | None = None,
    runtime_log_tailer: RuntimeLogTailer | None = None,
    on_connected=None,
) -> None:
    from websockets.sync.client import connect

    name = silicon_name(root)
    url = ws_url(config["server_url"])
    key = api_key_from_config(config)
    if not key:
        raise RuntimeError("Glass API key is unavailable.")
    print(f"[glass-agent] connecting to {config['server_url'].rstrip('/')}/ws/glass/agent/", flush=True)
    connect_options = {
        "close_timeout": 5,
        "open_timeout": 10,
        "additional_headers": {"X-Silicon-Key": key},
    }
    if url.lower().startswith("wss://"):
        connect_options["ssl"] = ssl_context()

    owned_reconciler = team_context_reconciler is None
    reconciler = team_context_reconciler or TeamContextReconciler(root)
    owned_trust_reconciler = trust_policy_reconciler is None
    trust_reconciler = trust_policy_reconciler or TrustPolicyReconciler(root)
    log_tailer = runtime_log_tailer or RuntimeLogTailer(root / ".silicon.log")
    try:
        with connect(url, **connect_options) as ws:
            workers: list[ChangeDrivenWorker] = []
            worker_failure: list[Exception] = []

            def worker_failed(exc: Exception) -> None:
                worker_failure.append(exc)
                try:
                    ws.close()
                except Exception:
                    pass

            try:
                print("[glass-agent] connected", flush=True)
                _request_team_context_reconcile(
                    reconciler,
                    force=True,
                    reason="websocket-connect",
                )
                _request_trust_reconcile(
                    trust_reconciler,
                    force=True,
                    reason="websocket-connect",
                )
                send_json(ws, {
                    "type": "handshake",
                    "name": name,
                    "version": local_version(root),
                    "hostname": os.uname().nodename if hasattr(os, "uname") else "",
                    "pid": os.getpid(),
                    "capabilities": ["trust_policy_v1"],
                })
                # Only now is the link proven usable end-to-end. The reconnect
                # policy times from here, so a socket that dies mid-handshake
                # counts as a failed attempt rather than a healthy link dropping.
                if on_connected is not None:
                    on_connected()

                last_status = status_payload(root)
                send_json(ws, last_status)

                from diagnostics.push import resolve_db_path

                diagnostic_path = Path(
                    resolve_db_path(root, config.get("diag_db"))
                )
                log_poll_lock = threading.Lock()
                diagnostic_drain_lock = threading.Lock()

                def poll_runtime_log() -> int:
                    with log_poll_lock:
                        return log_tailer.poll(
                            lambda frame: send_json(ws, frame)
                        )

                def drain_runtime_diagnostics() -> int:
                    with diagnostic_drain_lock:
                        return drain_diagnostics(ws, root, config)

                workers = [
                    ChangeDrivenWorker(
                        [root / ".silicon.log"],
                        poll_runtime_log,
                        fallback_seconds=60,
                        polling_seconds=1,
                        name="glass-runtime-log-follow",
                        on_error=worker_failed,
                    ),
                    ChangeDrivenWorker(
                        [
                            diagnostic_path,
                            Path(f"{diagnostic_path}-wal"),
                        ],
                        drain_runtime_diagnostics,
                        fallback_seconds=DIAGNOSTICS_INTERVAL,
                        polling_seconds=5,
                        name="glass-diagnostics-follow",
                        on_error=worker_failed,
                    ),
                ]
                now = time.monotonic()
                next_status_check = now + STATUS_CHECK_INTERVAL
                next_status_refresh = now + STATUS_REFRESH_INTERVAL
                next_ping = now + PING_INTERVAL
                next_team_context = now + TEAM_CONTEXT_INTERVAL
                next_trust_policy = now + TRUST_POLICY_INTERVAL
                first_loop_now = time.monotonic()

                for worker in workers:
                    worker.start()
                poll_runtime_log()
                drain_runtime_diagnostics()

                while running[0]:
                    if worker_failure:
                        raise worker_failure[0]
                    if first_loop_now is None:
                        now = time.monotonic()
                    else:
                        now = first_loop_now
                        first_loop_now = None
                    if now >= next_status_check:
                        current_status = status_payload(root)
                        if (
                            current_status != last_status
                            or now >= next_status_refresh
                        ):
                            send_json(ws, current_status)
                            last_status = current_status
                            next_status_refresh = (
                                now + STATUS_REFRESH_INTERVAL
                            )
                        next_status_check = (
                            now + STATUS_CHECK_INTERVAL
                        )
                    if now >= next_ping:
                        send_json(
                            ws,
                            {"type": "ping", "ts": int(time.time())},
                        )
                        next_ping = now + PING_INTERVAL
                    if now >= next_team_context:
                        _request_team_context_reconcile(
                            reconciler,
                            reason="websocket-safety",
                        )
                        next_team_context = now + TEAM_CONTEXT_INTERVAL
                    if now >= next_trust_policy:
                        _request_trust_reconcile(
                            trust_reconciler,
                            reason="websocket-safety",
                        )
                        next_trust_policy = now + TRUST_POLICY_INTERVAL

                    deadline = min(
                        next_status_check,
                        next_ping,
                        next_team_context,
                        next_trust_policy,
                    )
                    try:
                        raw = ws.recv(
                            timeout=max(
                                0.05,
                                deadline - now,
                            )
                        )
                    except TimeoutError:
                        continue
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, dict):
                        handle_message(
                            ws,
                            msg,
                            root,
                            name,
                            config,
                            team_context_reconciler=reconciler,
                            trust_policy_reconciler=trust_reconciler,
                        )
            finally:
                for worker in workers:
                    worker.stop()
    finally:
        terminal_stop()
        if owned_reconciler:
            reconciler.stop()
        if owned_trust_reconciler:
            trust_reconciler.stop()


def main() -> None:
    root = silicon_dir()
    prepend_local_bin(root)
    config = load_config(root)
    if not config:
        print("[glass-agent] No .glass.json found. Exiting.", flush=True)
        sys.exit(1)
    if not config.get("server_url") or not api_key_from_config(config):
        print("[glass-agent] Missing server_url or api_key in .glass.json. Exiting.", flush=True)
        sys.exit(1)
    pid_file = root / ".glass_agent.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    running = [True]

    def stop(_signum, _frame):
        running[0] = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    backoff = 1
    reconciler = TeamContextReconciler(root)
    trust_reconciler = TrustPolicyReconciler(root)
    runtime_log_tailer = RuntimeLogTailer(root / ".silicon.log")
    print(f"[glass-agent] started for '{silicon_name(root)}'", flush=True)
    try:
        while running[0]:
            config = load_config(root)
            key = api_key_from_config(config)
            if not config.get("server_url") or not key:
                print("[glass-agent] credentials unavailable; checking again in 300s", flush=True)
                wait_for_retry(
                    root,
                    running,
                    AUTH_REJECTION_BACKOFF,
                    key,
                    str(config.get("server_url") or ""),
                )
                continue
            connected_at = None

            def mark_connected():
                nonlocal connected_at
                connected_at = time.monotonic()

            try:
                run_live(
                    root,
                    config,
                    running,
                    team_context_reconciler=reconciler,
                    trust_policy_reconciler=trust_reconciler,
                    runtime_log_tailer=runtime_log_tailer,
                    on_connected=mark_connected,
                )
                backoff = 1
            except Exception as exc:
                if running[0]:
                    rejected = is_authentication_rejection(exc)
                    delay, next_backoff = reconnect_delay(
                        backoff,
                        rejected=rejected,
                        session_seconds=(
                            None
                            if connected_at is None
                            else time.monotonic() - connected_at
                        ),
                    )
                    reason = "authentication rejected" if rejected else str(exc)
                    print(f"[glass-agent] disconnected: {reason}; reconnecting in {delay}s", flush=True)
                    wait_for_retry(
                        root,
                        running,
                        delay,
                        key if rejected else "",
                        str(config.get("server_url") or "") if rejected else None,
                    )
                    backoff = next_backoff
    finally:
        reconciler.stop()
        trust_reconciler.stop()
        pid_file.unlink(missing_ok=True)
    print("[glass-agent] stopped", flush=True)


if __name__ == "__main__":
    main()
