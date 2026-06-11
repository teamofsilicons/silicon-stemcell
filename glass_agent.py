#!/usr/bin/env python3
"""Glass sidecar for Silicon v1.

Keeps one live connection to Glass control, reports status, and runs manifest
backups when Glass asks for them.
"""
from __future__ import annotations

import json
import os
import signal
import ssl
import subprocess
import sys
import time
from pathlib import Path

STATUS_INTERVAL = 15
PING_INTERVAL = 20
MAX_BACKOFF = 30


def silicon_dir() -> Path:
    return Path(__file__).resolve().parent


def load_config(root: Path) -> dict:
    path = root / ".glass.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def silicon_name(root: Path) -> str:
    path = root / "silicon.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("address") or data.get("name") or root.name
        except Exception:
            pass
    return root.name


def local_version(root: Path) -> str:
    path = root / "silicon.info"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("version", "")
        except Exception:
            pass
    return ""


def ws_url(server_url: str, api_key: str) -> str:
    base = server_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[8:]
    elif base.startswith("http://"):
        base = "ws://" + base[7:]
    sep = "&" if "?" in base else "?"
    return f"{base}/ws/glass/agent/{sep}silicon_key={api_key}"


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def detect_status(root: Path) -> str:
    pid_file = root / ".silicon.pid"
    stop_file = root / ".silicon.stop"
    if not pid_file.exists():
        return "stopped"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return "running"
    except (ValueError, ProcessLookupError, PermissionError):
        return "stopped" if stop_file.exists() else "crashed"


def send_json(ws, payload: dict) -> None:
    ws.send(json.dumps(payload, separators=(",", ":")))


def status_payload(root: Path) -> dict:
    return {
        "type": "status",
        "status": detect_status(root),
        "version": local_version(root),
        "pid": os.getpid(),
    }


def run_backup(root: Path, note: str = "glass command") -> tuple[str, str]:
    try:
        from core.backup import run_backup as manifest_backup

        ok = manifest_backup(root, note=note, logger=lambda msg: print(f"[glass-agent] {msg}", flush=True))
        return ("done", "backup complete") if ok else ("failed", "backup skipped")
    except Exception as exc:
        return "failed", str(exc)


def execute_command(command: dict, root: Path, name: str) -> tuple[str, str]:
    action = command.get("command", "")
    if action in {"backup", "backup_now"}:
        return run_backup(root, note=f"glass command {command.get('id') or ''}".strip())
    if action == "start":
        try:
            subprocess.Popen(["silicon", "start", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return "done", "started"
        except Exception as exc:
            return "failed", str(exc)
    if action == "stop":
        try:
            subprocess.Popen(["silicon", "stop", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return "done", "stopped"
        except Exception as exc:
            return "failed", str(exc)
    if action == "version":
        # Report the version this silicon is currently running (silicon.info).
        return "done", local_version(root) or "unversioned"
    if action in {"fetch_latest", "update_check"}:
        # Brain-driven update: force the version check now. When behind, it
        # spawns the detached update brain which manages the whole sequence
        # itself (diff, apply, bump silicon.info) — works while running.
        try:
            proc = subprocess.run(
                ["silicon", "update", "check", name],
                capture_output=True, text=True, timeout=180,
            )
            output = proc.stdout.strip() or proc.stderr.strip()
            return ("done" if proc.returncode == 0 else "failed"), output or "update check triggered"
        except Exception as exc:
            return "failed", str(exc)
    if action == "update":
        # Legacy mechanical update (CLI merge). A running silicon is stopped
        # for the update and restarted after, so Glass can push a release to
        # the whole fleet in one go.
        was_running = detect_status(root) == "running"
        if was_running:
            try:
                subprocess.run(["silicon", "stop", name], capture_output=True, text=True, timeout=60)
            except Exception as exc:
                return "failed", f"could not stop before update: {exc}"
            for _ in range(30):
                if detect_status(root) != "running":
                    break
                time.sleep(1)
            if detect_status(root) == "running":
                return "failed", "silicon did not stop before update"
        try:
            proc = subprocess.run(["silicon", "update", name], capture_output=True, text=True, timeout=300)
            output = proc.stdout.strip() or proc.stderr.strip()
            ok = proc.returncode == 0
        except Exception as exc:
            return "failed", str(exc)
        if was_running:
            try:
                subprocess.Popen(["silicon", "start", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                output = f"{output} · restarted".strip(" ·")
            except Exception as exc:
                return ("done" if ok else "failed"), f"{output} (restart failed: {exc})"
        return ("done" if ok else "failed"), output
    return "failed", f"unknown command: {action}"


def handle_message(ws, msg: dict, root: Path, name: str) -> None:
    msg_type = msg.get("type")
    if msg_type == "welcome":
        print("[glass-agent] welcome", flush=True)
        return
    if msg_type == "billing":
        print(f"[glass-agent] billing: {msg.get('status') or msg.get('message') or 'ok'}", flush=True)
        return
    if msg_type == "pong":
        return
    if msg_type != "command":
        return

    command_id = msg.get("id", "")
    if command_id:
        send_json(ws, {"type": "command_ack", "id": command_id, "command": msg.get("command", "")})
    status, detail = execute_command(msg, root, name)
    # Keep Glass's stored status fresh — the console reads `version` from it
    # (the on-demand "version" command, and any command that may change it).
    send_json(ws, {"type": "status", "version": local_version(root)})
    if command_id:
        send_json(ws, {
            "type": "command_result",
            "id": command_id,
            "command": msg.get("command", ""),
            "status": status,
            "message": detail,
        })
    print(f"[glass-agent] command {msg.get('command')} -> {status}: {detail}", flush=True)


def run_live(root: Path, config: dict, running: list[bool]) -> None:
    from websockets.sync.client import connect

    name = silicon_name(root)
    url = ws_url(config["server_url"], config["api_key"])
    print(f"[glass-agent] connecting to {config['server_url'].rstrip('/')}/ws/glass/agent/", flush=True)
    with connect(url, close_timeout=5, open_timeout=10, ssl=ssl_context()) as ws:
        print("[glass-agent] connected", flush=True)
        send_json(ws, {
            "type": "handshake",
            "name": name,
            "version": local_version(root),
            "hostname": os.uname().nodename if hasattr(os, "uname") else "",
            "pid": os.getpid(),
        })
        send_json(ws, status_payload(root))
        next_status = time.time() + STATUS_INTERVAL
        next_ping = time.time() + PING_INTERVAL

        while running[0]:
            now = time.time()
            if now >= next_status:
                send_json(ws, status_payload(root))
                next_status = now + STATUS_INTERVAL
            if now >= next_ping:
                send_json(ws, {"type": "ping", "ts": int(now)})
                next_ping = now + PING_INTERVAL

            try:
                raw = ws.recv(timeout=2)
            except TimeoutError:
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                handle_message(ws, msg, root, name)


def main() -> None:
    root = silicon_dir()
    config = load_config(root)
    if not config:
        print("[glass-agent] No .glass.json found. Exiting.", flush=True)
        sys.exit(1)
    if not config.get("server_url") or not config.get("api_key"):
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
    print(f"[glass-agent] started for '{silicon_name(root)}'", flush=True)
    while running[0]:
        try:
            run_live(root, config, running)
            backoff = 1
        except Exception as exc:
            if running[0]:
                print(f"[glass-agent] disconnected: {exc}; reconnecting in {backoff}s", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    pid_file.unlink(missing_ok=True)
    print("[glass-agent] stopped", flush=True)


if __name__ == "__main__":
    main()
