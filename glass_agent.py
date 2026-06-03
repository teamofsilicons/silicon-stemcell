#!/usr/bin/env python3
"""Glass Agent — sidecar daemon that connects a silicon instance to Glass remote control.
Tries WebSocket for real-time streaming, falls back to REST polling."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

POLL_INTERVAL = 10
WS_HEARTBEAT_INTERVAL = 10
WS_LOG_INTERVAL = 1  # check for new logs every second
LOG_CHUNK_MAX = 50_000  # bytes
REST_FALLBACK_CYCLES = 5  # poll cycles before retrying WS
VERSION_CHECK_INTERVAL = 1800  # check for updates every 30 minutes
UPSTREAM_VERSION_URL = "https://raw.githubusercontent.com/unlikefraction/silicon-stemcell/main/silicon.info"

# ── Config ───────────────────────────────────────────────────


def find_silicon_dir():
    return Path(__file__).resolve().parent


def load_config(silicon_dir):
    config_path = silicon_dir / ".glass.json"
    if not config_path.exists():
        return None
    with open(config_path) as f:
        return json.load(f)


def get_silicon_name(silicon_dir):
    sj = silicon_dir / "silicon.json"
    if sj.exists():
        try:
            data = json.loads(sj.read_text())
            return data.get("address") or data.get("name") or silicon_dir.name
        except Exception:
            pass
    return silicon_dir.name


def build_ws_url(server_url):
    """Convert https://... to wss://... /ws/agent/"""
    url = server_url.rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[8:] + "/ws/agent/"
    elif url.startswith("http://"):
        return "ws://" + url[7:] + "/ws/agent/"
    return None


# ── HTTP helpers ─────────────────────────────────────────────


def api_request(server_url, path, api_key, method="GET", data=None):
    import urllib.error
    import urllib.request

    url = server_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {api_key}"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


# ── Version checking ─────────────────────────────────────────


def get_local_version(silicon_dir):
    info = silicon_dir / "silicon.info"
    if info.exists():
        try:
            return json.loads(info.read_text()).get("version", "")
        except Exception:
            pass
    return ""


_cached_latest_version = ""
_last_version_check = 0


def get_latest_version():
    global _cached_latest_version, _last_version_check
    now = time.time()
    if now - _last_version_check < VERSION_CHECK_INTERVAL and _cached_latest_version:
        return _cached_latest_version
    try:
        import urllib.request
        req = urllib.request.Request(UPSTREAM_VERSION_URL, headers={"User-Agent": "glass-agent"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            _cached_latest_version = data.get("version", "")
            _last_version_check = now
    except Exception:
        pass
    return _cached_latest_version


# ── Status detection ─────────────────────────────────────────


def detect_status(silicon_dir):
    pid_file = silicon_dir / ".silicon.pid"
    stop_file = silicon_dir / ".silicon.stop"
    if not pid_file.exists():
        return "stopped"
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return "running"
    except (ValueError, ProcessLookupError, PermissionError):
        if stop_file.exists():
            return "stopped"
        return "crashed"


def detect_backup_running(silicon_dir):
    pid_file = silicon_dir / ".glass-push.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


# ── Command execution ────────────────────────────────────────


def execute_command(cmd, silicon_name):
    action = cmd.get("command", "")
    silicon_dir = find_silicon_dir()
    try:
        if action == "start":
            subprocess.Popen(
                ["silicon", "start", silicon_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(30):
                time.sleep(1)
                if silicon_dir and detect_status(silicon_dir) == "running":
                    return "done", "started"
            return "done", "started (status unconfirmed)"

        elif action == "stop":
            subprocess.Popen(
                ["silicon", "stop", silicon_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(30):
                time.sleep(1)
                if silicon_dir and detect_status(silicon_dir) != "running":
                    return "done", "stopped"
            return "done", "stopped (status unconfirmed)"

        elif action == "backup_now":
            result = subprocess.run(
                ["silicon", "push", silicon_name, "now"],
                capture_output=True, text=True, timeout=120,
            )
            msg = result.stdout.strip() or result.stderr.strip() or "backup complete"
            return ("done" if result.returncode == 0 else "failed"), msg

        elif action == "backup_on":
            if detect_backup_running(silicon_dir):
                return "done", "continuous backup already running"
            subprocess.Popen(
                ["silicon", "push", silicon_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Wait a few seconds for the PID file to appear
            for _ in range(10):
                time.sleep(1)
                if detect_backup_running(silicon_dir):
                    return "done", "continuous backup started"
            return "done", "continuous backup started (status unconfirmed)"

        elif action == "backup_off":
            result = subprocess.run(
                ["silicon", "push", silicon_name, "stop"],
                capture_output=True, text=True, timeout=15,
            )
            msg = result.stdout.strip() or result.stderr.strip() or "continuous backup stopped"
            return "done", msg

        elif action == "update":
            if detect_status(silicon_dir) == "running":
                return "failed", "bud must be stopped before updating"
            result = subprocess.run(
                ["silicon", "update", silicon_name],
                capture_output=True, text=True, timeout=300,
            )
            output = result.stdout.strip()
            err = result.stderr.strip()
            if result.returncode == 0:
                return "done", output or "updated successfully"
            elif result.returncode == 2:
                return "failed", "merge conflicts detected — no files were changed"
            else:
                return "failed", err or output or "update failed"

        else:
            return "failed", f"unknown command: {action}"
    except subprocess.TimeoutExpired:
        return "failed", "command timed out"
    except FileNotFoundError:
        return "failed", "silicon CLI not found on PATH"
    except Exception as e:
        return "failed", str(e)


# ── Log streaming ────────────────────────────────────────────


class LogTailer:
    def __init__(self, log_path):
        self.path = log_path
        self.pos = 0
        if self.path.exists():
            self.pos = self.path.stat().st_size

    def read_new(self):
        if not self.path.exists():
            self.pos = 0
            return ""
        size = self.path.stat().st_size
        if size < self.pos:
            self.pos = 0
        if size == self.pos:
            return ""
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                chunk = f.read(LOG_CHUNK_MAX)
                self.pos = f.tell()
            return chunk.decode("utf-8", errors="replace")
        except Exception:
            return ""


# ── WebSocket mode ───────────────────────────────────────────


def run_websocket_loop(ws_url, api_key, silicon_name, silicon_dir, log_tailer, running_flag):
    """Connect via WebSocket for real-time relay. Raises on disconnect."""
    from websockets.sync.client import connect

    print(f"[glass-agent] Connecting to {ws_url}", flush=True)
    with connect(ws_url, close_timeout=5, open_timeout=10) as ws:
        # Auth
        ws.send(json.dumps({"type": "auth", "token": api_key}))
        resp = json.loads(ws.recv(timeout=5))
        if resp.get("type") != "auth_ok":
            raise ConnectionError(f"Auth failed: {resp.get('reason', 'unknown')}")

        print(f"[glass-agent] WebSocket connected (live mode)", flush=True)

        # Background threads for heartbeats and logs
        stop_event = threading.Event()
        ws_lock = threading.Lock()

        def safe_send(data):
            with ws_lock:
                ws.send(data)

        def heartbeat_sender():
            while not stop_event.is_set() and running_flag[0]:
                try:
                    status = detect_status(silicon_dir)
                    backup = detect_backup_running(silicon_dir)
                    cur_ver = get_local_version(silicon_dir)
                    lat_ver = get_latest_version()
                    safe_send(json.dumps({
                        "type": "heartbeat", "status": status,
                        "backup_running": backup,
                        "current_version": cur_ver,
                        "latest_version": lat_ver,
                    }))
                except Exception:
                    break
                stop_event.wait(WS_HEARTBEAT_INTERVAL)

        def log_sender():
            while not stop_event.is_set() and running_flag[0]:
                try:
                    new_lines = log_tailer.read_new()
                    if new_lines:
                        safe_send(json.dumps({"type": "log", "lines": new_lines}))
                except Exception:
                    break
                stop_event.wait(WS_LOG_INTERVAL)

        t_hb = threading.Thread(target=heartbeat_sender, daemon=True)
        t_log = threading.Thread(target=log_sender, daemon=True)
        t_hb.start()
        t_log.start()

        try:
            # Receiver: blocks on ws.recv(), handles commands
            while running_flag[0]:
                try:
                    raw = ws.recv(timeout=2)
                except TimeoutError:
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get("type") == "command":
                    cmd_id = msg.get("id", "")
                    # ACK
                    safe_send(json.dumps({"type": "command_ack", "id": cmd_id, "command": msg.get("command", "")}))
                    # Execute
                    result_status, result_msg = execute_command(msg, silicon_name)
                    safe_send(json.dumps({
                        "type": "command_result",
                        "id": cmd_id,
                        "command": msg.get("command", ""),
                        "status": result_status,
                        "message": result_msg,
                    }))
                    print(f"[glass-agent] {msg.get('command')} → {result_status}: {result_msg}", flush=True)
        finally:
            stop_event.set()
            t_hb.join(timeout=3)
            t_log.join(timeout=3)


# ── REST polling mode (fallback) ─────────────────────────────


def run_poll_loop(server_url, api_key, silicon_name, silicon_dir, log_tailer, running_flag, max_cycles=0):
    """REST polling fallback. Runs for max_cycles (0 = unlimited)."""
    cycle = 0
    seconds_in_cycle = 0
    while running_flag[0]:
        try:
            # Heartbeat + commands every POLL_INTERVAL seconds
            if seconds_in_cycle == 0:
                status = detect_status(silicon_dir)
                backup = detect_backup_running(silicon_dir)
                cur_ver = get_local_version(silicon_dir)
                lat_ver = get_latest_version()
                api_request(server_url, "/control/api/heartbeat/", api_key, method="POST", data={
                    "status": status, "backup_running": backup,
                    "current_version": cur_ver, "latest_version": lat_ver,
                })

                resp = api_request(server_url, "/control/api/commands/pending/", api_key)
                commands = resp.get("commands", []) if isinstance(resp, dict) else []
                for cmd in commands:
                    cmd_id = cmd.get("id")
                    if not cmd_id:
                        continue
                    api_request(server_url, f"/control/api/commands/{cmd_id}/ack/", api_key, method="POST")
                    result_status, result_msg = execute_command(cmd, silicon_name)
                    api_request(server_url, f"/control/api/commands/{cmd_id}/complete/", api_key, method="POST", data={
                        "status": result_status, "message": result_msg,
                    })
                    print(f"[glass-agent] {cmd.get('command')} → {result_status}: {result_msg}", flush=True)

            # Logs every 2 seconds
            new_lines = log_tailer.read_new()
            if new_lines:
                api_request(server_url, "/control/api/logs/", api_key, method="POST", data={"lines": new_lines})

        except Exception as e:
            print(f"[glass-agent] Poll error: {e}", flush=True)

        seconds_in_cycle += 2
        if seconds_in_cycle >= POLL_INTERVAL:
            seconds_in_cycle = 0
            cycle += 1
            if max_cycles and cycle >= max_cycles:
                return

        if not running_flag[0]:
            return
        time.sleep(2)


# ── Main ─────────────────────────────────────────────────────


def main():
    silicon_dir = find_silicon_dir()
    config = load_config(silicon_dir)
    if not config:
        print("[glass-agent] No .glass.json found. Exiting.", flush=True)
        sys.exit(1)

    server_url = config.get("server_url", "")
    api_key = config.get("api_key", "")
    if not server_url or not api_key:
        print("[glass-agent] Missing server_url or api_key in .glass.json. Exiting.", flush=True)
        sys.exit(1)

    silicon_name = get_silicon_name(silicon_dir)
    log_tailer = LogTailer(silicon_dir / ".silicon.log")
    agent_pid_file = silicon_dir / ".glass_agent.pid"
    agent_pid_file.write_text(str(os.getpid()))

    running = [True]  # mutable for threads

    def handle_signal(signum, frame):
        running[0] = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    print(f"[glass-agent] Started for '{silicon_name}' → {server_url}", flush=True)

    # Try WebSocket, fall back to REST
    ws_url = build_ws_url(server_url)
    ws_available = False
    try:
        import websockets  # noqa: F401
        ws_available = ws_url is not None
    except ImportError:
        print("[glass-agent] websockets not installed, using REST polling only.", flush=True)

    while running[0]:
        if ws_available:
            try:
                run_websocket_loop(ws_url, api_key, silicon_name, silicon_dir, log_tailer, running)
            except Exception as e:
                if running[0]:
                    print(f"[glass-agent] WS disconnected: {e}. Falling back to REST.", flush=True)
                    run_poll_loop(server_url, api_key, silicon_name, silicon_dir, log_tailer, running, max_cycles=REST_FALLBACK_CYCLES)
        else:
            run_poll_loop(server_url, api_key, silicon_name, silicon_dir, log_tailer, running)

    try:
        agent_pid_file.unlink(missing_ok=True)
    except Exception:
        pass
    print("[glass-agent] Stopped.", flush=True)


if __name__ == "__main__":
    main()
