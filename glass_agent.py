#!/usr/bin/env python3
"""Glass sidecar for Silicon v1.

Keeps one live connection to Glass control, reports status, and runs manifest
backups when Glass asks for them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import shutil
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from core.runtime_paths import CODE_ROOT, DATA_ROOT

try:
    import pty
except ImportError:  # Windows has no pseudo-terminal module.
    pty = None

STATUS_INTERVAL = 15
PING_INTERVAL = 20
DIAGNOSTICS_INTERVAL = 5
TEAM_CONTEXT_INTERVAL = 60
TRUST_POLICY_INTERVAL = 60
RUNTIME_LOG_INITIAL_LINES = 10
RUNTIME_LOG_BATCH_LINES = 100
RUNTIME_LOG_MAX_LINE_BYTES = 16 * 1024
RUNTIME_LOG_INITIAL_SCAN_BYTES = 256 * 1024
RUNTIME_LOG_ANCHOR_BYTES = 64
MAX_BACKOFF = 30
AUTH_REJECTION_BACKOFF = 5 * 60
REGISTRY_TIMEOUT = 8
NPM_LIST_TIMEOUT = 12
NPM_RUNTIME_PACKAGES = (
    {"name": "@anthropic-ai/claude-code", "command": "claude"},
    {"name": "@openai/codex", "command": "codex"},
)
LOCAL_NPM_CLIS = (
    {
        "name": "@teamofsilicons/silicon-interface-cli",
        "label": "silicon-interface",
        "commands": (".silicon-interface/bin/si", "si", "silicon-interface"),
        "install_command": "silicon-interface",
    },
)
SCRIPT_CLIS = (
    {
        "name": "silicon",
        "command": "silicon",
        "source": "silicon CLI",
        "package": "silicon-cli",
        "update_args": ("script", "update"),
    },
    {
        "name": "silicon-browser",
        "command": "silicon-browser",
        "source": "Silicon Browser CLI",
        "package": "silicon-browser",
        "update_kind": "python_cli",
    },
)
TERMINAL_COMMANDS = {
    "claude": ("claude",),
    "codex": ("codex", "login"),
}
SEND_LOCK = threading.Lock()
TERMINAL_LOCK = threading.Lock()
TERMINAL_SESSION: dict[str, object] = {}
DIAGNOSTIC_RECOVERY_CHECK_INTERVAL = 60
DIAGNOSTIC_RECOVERY_CHECKS: dict[tuple[str, str, int | None], float] = {}


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
                from core.team_context import reconcile_team_context
                from core.maintenance import (
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
                from core.maintenance import MaintenanceCoordinator, heartbeat_scope
                from core.trust import reconcile_trust_policy

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


def silicon_dir() -> Path:
    return DATA_ROOT


def release_dir(root: Path) -> Path:
    """Return active code for the real instance, preserving explicit test roots."""

    try:
        return CODE_ROOT if Path(root).resolve() == DATA_ROOT else Path(root)
    except OSError:
        return Path(root)


def local_bin_dir(root: Path) -> Path:
    return root / ".local" / "bin"


def prepend_local_bin(root: Path) -> None:
    bin_dir = str(local_bin_dir(root))
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in parts:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def load_config(root: Path) -> dict:
    # Share the runtime's exact-root, no-symlink loader so the sidecar cannot
    # inherit another Silicon's credentials and legacy 0644 files are hardened
    # to owner-only permissions before use.
    from core.glass import load_glass_config

    try:
        config, _path = load_glass_config(root)
    except FileNotFoundError:
        return {}
    return config


def glass_api_key(config: dict) -> str:
    """Return either supported spelling of the per-Silicon credential."""
    return str(config.get("api_key") or config.get("silicon_api_key") or "").strip()


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
    path = release_dir(root) / "silicon.info"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("version", "")
        except Exception:
            pass
    return ""


def ws_url(server_url: str) -> str:
    """Build a credential-safe Glass agent URL.

    The agent always authenticates this socket with a permanent Silicon key, so
    plaintext WebSockets are allowed only for loopback development servers.
    Reject URL features that could obscure the authenticated destination.
    """

    from core.glass import GlassConfigurationError, validate_authenticated_origin

    try:
        validated = validate_authenticated_origin(server_url)
        parsed = urlsplit(validated)
    except (GlassConfigurationError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Refusing to send a Silicon API key to an unsafe Glass WebSocket URL."
        ) from exc

    websocket_scheme = "wss" if parsed.scheme.lower() == "https" else "ws"
    websocket_path = f"{parsed.path.rstrip('/')}/ws/glass/agent/"
    return urlunsplit(
        parsed._replace(
            scheme=websocket_scheme,
            path=websocket_path,
            query="",
            fragment="",
        )
    )


def is_authentication_rejection(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    return status in {401, 403}


def wait_for_retry(
    root: Path,
    running: list[bool],
    delay: int,
    rejected_key: str = "",
    rejected_server_url: str | None = None,
) -> None:
    """Wait interruptibly, optionally waking when credentials are repaired."""

    deadline = time.monotonic() + max(0, delay)
    while running[0] and time.monotonic() < deadline:
        try:
            current_config = load_config(root)
            current_key = glass_api_key(current_config)
            current_server_url = str(current_config.get("server_url") or "")
        except (OSError, ValueError, TypeError):
            current_key = ""
            current_server_url = ""
        if current_key and current_server_url:
            if rejected_server_url is not None and (
                not secrets.compare_digest(current_key, rejected_key)
                or current_server_url != rejected_server_url
            ):
                return
            if rejected_key and not secrets.compare_digest(current_key, rejected_key):
                return
        time.sleep(min(1, max(0, deadline - time.monotonic())))


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
    with SEND_LOCK:
        ws.send(json.dumps(payload, separators=(",", ":")))


def runtime_log_level(line: str) -> str:
    """Infer a useful Glass display level without changing the log text."""

    lowered = line.lower()
    if any(
        marker in lowered
        for marker in ("error", "exception", "traceback", "fatal", "failed")
    ):
        return "error"
    if "warning" in lowered or "warn" in lowered:
        return "warn"
    return "info"


class RuntimeLogTailer:
    """Incrementally mirror the same process log shown by ``silicon debug``.

    The cursor lives for the lifetime of the Glass sidecar, rather than for one
    WebSocket, so a reconnect catches up without replaying already-sent lines.
    File identity, size, and a short byte anchor make normal replacement and
    copy-truncate rotation safe.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._identity: tuple[int, int] | None = None
        self._position: int | None = None
        self._anchor = b""

    @staticmethod
    def _file_identity(metadata) -> tuple[int, int]:
        return int(metadata.st_dev), int(metadata.st_ino)

    @staticmethod
    def _read_anchor(handle, position: int) -> bytes:
        length = min(max(0, position), RUNTIME_LOG_ANCHOR_BYTES)
        if not length:
            return b""
        handle.seek(position - length)
        return handle.read(length)

    @staticmethod
    def _initial_position(handle, size: int) -> int:
        """Match ``tail -f`` by starting with at most ten recent lines."""

        start = max(0, size - RUNTIME_LOG_INITIAL_SCAN_BYTES)
        handle.seek(start)
        sample = handle.read(size - start)
        if start:
            newline = sample.find(b"\n")
            if newline < 0:
                return size
            start += newline + 1
            sample = sample[newline + 1 :]
        lines = sample.splitlines(keepends=True)
        return start + len(sample) - sum(
            len(line) for line in lines[-RUNTIME_LOG_INITIAL_LINES:]
        )

    def _prepare(self, handle) -> None:
        metadata = os.fstat(handle.fileno())
        identity = self._file_identity(metadata)
        size = int(metadata.st_size)

        if self._identity != identity:
            self._identity = identity
            self._position = (
                self._initial_position(handle, size)
                if self._position is None
                else 0
            )
            self._anchor = self._read_anchor(handle, self._position)
            return

        position = int(self._position or 0)
        replaced = size < position
        if not replaced and self._anchor:
            replaced = self._read_anchor(handle, position) != self._anchor
        if replaced:
            self._position = 0
            self._anchor = b""

    def poll(self, send) -> int:
        """Send newly completed log lines as bounded Glass log frames."""

        try:
            handle = open(self.path, "rb")
        except (FileNotFoundError, IsADirectoryError, OSError):
            return 0

        sent = 0
        with handle:
            self._prepare(handle)
            handle.seek(int(self._position or 0))
            while sent < RUNTIME_LOG_BATCH_LINES:
                raw = handle.readline(RUNTIME_LOG_MAX_LINE_BYTES + 1)
                if not raw:
                    break

                complete = raw.endswith((b"\n", b"\r"))
                if not complete and len(raw) <= RUNTIME_LOG_MAX_LINE_BYTES:
                    # Do not show a process write until its line is complete.
                    handle.seek(int(self._position or 0))
                    break

                omitted = 0
                if len(raw) > RUNTIME_LOG_MAX_LINE_BYTES:
                    kept = raw[:RUNTIME_LOG_MAX_LINE_BYTES]
                    omitted = len(raw) - len(kept)
                    raw = kept
                    while not complete:
                        remainder = handle.readline(RUNTIME_LOG_MAX_LINE_BYTES)
                        if not remainder:
                            break
                        omitted += len(remainder)
                        complete = remainder.endswith((b"\n", b"\r"))

                line = raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
                if omitted:
                    line += f" …(+{omitted} bytes truncated)"
                frame = {
                    "type": "log",
                    "level": runtime_log_level(line),
                    "source": "silicon",
                    "ts": now_iso(),
                    "msg": line,
                }
                send(frame)
                self._position = handle.tell()
                self._anchor = self._read_anchor(handle, self._position)
                handle.seek(self._position)
                sent += 1
        return sent


def drain_diagnostics(ws, root: Path, config: dict) -> int:
    """Push completed traces over the authenticated agent socket, fail-open."""
    if config.get("diag_push", True) is False:
        return 0
    coordinator = None
    activity = None
    heartbeat_context = None
    try:
        from core.maintenance import MaintenanceCoordinator, heartbeat_scope

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
        from core.glass_diag_push import (
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def status_payload(root: Path) -> dict:
    return {
        "type": "status",
        "status": detect_status(root),
        "version": local_version(root),
        "pid": os.getpid(),
    }


def run_backup(root: Path, note: str = "glass command") -> tuple[str, str]:
    coordinator = None
    activity = None
    try:
        from core.maintenance import (
            MaintenanceCoordinator,
            heartbeat_scope,
        )

        coordinator = MaintenanceCoordinator(root)
        activity = coordinator.acquire_activity(
            "backup",
            activity_id="glass-backup",
        )
        if activity is None:
            return "failed", "Silicon is preparing an update; backup start is deferred."
        from core.backup import run_backup as manifest_backup

        with heartbeat_scope([activity], coordinator=coordinator):
            ok = manifest_backup(
                root,
                note=note,
                logger=lambda msg: print(f"[glass-agent] {msg}", flush=True),
            )
        return ("done", "backup complete") if ok else ("failed", "backup skipped")
    except Exception as exc:
        return "failed", str(exc)
    finally:
        if coordinator is not None and activity is not None:
            coordinator.release(activity)


def _request_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": "silicon-glass-agent/1.0"})
    with urlopen(req, timeout=REGISTRY_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _latest_pypi_version(name: str) -> tuple[str, str]:
    try:
        body = _request_json(f"https://pypi.org/pypi/{quote(name)}/json")
        return str((body.get("info") or {}).get("version") or ""), ""
    except Exception as exc:
        return "", str(exc)


def _latest_npm_version(name: str) -> tuple[str, str]:
    try:
        body = _request_json(f"https://registry.npmjs.org/{quote(name, safe='')}/latest")
        return str(body.get("version") or ""), ""
    except Exception as exc:
        return "", str(exc)


def _lookup_many(names: list[str], lookup) -> dict[str, tuple[str, str]]:
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name and name not in seen:
            unique.append(name)
            seen.add(name)
    if not unique:
        return {}

    results: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(unique))) as pool:
        futures = {pool.submit(lookup, name): name for name in unique}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = "", str(exc)
    return results


def _latest_github_main(repo: str) -> tuple[str, str]:
    try:
        body = _request_json(f"https://api.github.com/repos/{repo}/commits/main")
        sha = str(body.get("sha") or "")
        return (f"main@{sha[:12]}" if sha else ""), ""
    except Exception as exc:
        return "", str(exc)


def _requirement_name(line: str) -> str:
    line = (line or "").split("#", 1)[0].split(";", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http://", "https://")):
        return ""
    match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[.*?\])?", line)
    return match.group(1) if match else ""


def _python_requirements(root: Path) -> list[tuple[str, str]]:
    req = root / "requirements.txt"
    if not req.exists():
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in req.read_text(encoding="utf-8").splitlines():
        name = _requirement_name(raw)
        key = name.lower().replace("_", "-")
        if name and key not in seen:
            seen.add(key)
            out.append((name, raw.strip()))
    return out


def _installed_python_version(name: str) -> str:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return ""


def _npm_global_versions() -> tuple[dict[str, str], str]:
    npm = shutil.which("npm")
    if not npm:
        return {}, "npm not found"
    try:
        proc = subprocess.run(
            [npm, "list", "-g", "--depth=0", "--json"],
            capture_output=True,
            text=True,
            timeout=NPM_LIST_TIMEOUT,
        )
        body = json.loads(proc.stdout or "{}")
        deps = body.get("dependencies") or {}
        return {
            name: str((info or {}).get("version") or "")
            for name, info in deps.items()
            if isinstance(info, dict)
        }, ""
    except Exception as exc:
        return {}, str(exc)


def _version_from_command(command: str) -> str:
    exe = command if os.path.sep in command else shutil.which(command)
    if not exe:
        return ""
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return ""
    text = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not text:
        return ""
    match = re.search(r"\d+(?:\.\d+)+(?:[-+][A-Za-z0-9_.-]+)?", text[0])
    return match.group(0) if match else text[0][:80]


def _python_runner_from_executable(exe: str) -> list[str]:
    try:
        first_line = Path(exe).read_bytes()[:256].splitlines()[0].decode("utf-8", errors="ignore")
    except Exception:
        return []
    if first_line.startswith("#!") and "python" in first_line.lower():
        try:
            parts = shlex.split(first_line[2:].strip())
        except ValueError:
            parts = first_line[2:].strip().split()
        if parts:
            runner = parts[:]
            if Path(runner[0]).name == "env" and len(runner) == 1:
                runner.append("python3")
            return runner
    return []


def _python_console_package_version(root: Path, command: str, package: str) -> str:
    exe = _resolve_command(root, command)
    if not exe:
        return ""

    code = (
        "from importlib.metadata import PackageNotFoundError, version\n"
        f"try: print(version({package!r}))\n"
        "except PackageNotFoundError: pass\n"
    )
    runner = _python_runner_from_executable(exe)
    if runner:
        try:
            proc = subprocess.run(
                [*runner, "-c", code],
                capture_output=True,
                text=True,
                timeout=15,
            )
            text = (proc.stdout or "").strip().splitlines()
            if proc.returncode == 0 and text:
                return text[0]
        except Exception:
            pass

    return _installed_python_version(package)


def _resolve_command(root: Path, command: str) -> str:
    path = root / command
    if os.path.sep in command and path.exists():
        return str(path)
    found = shutil.which(command)
    return found or ""


def _file_identity(path: str) -> str:
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return ""
        digest = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
        return f"sha256:{digest}"
    except Exception:
        return ""


def _command_identity(root: Path, command: str) -> str:
    exe = _resolve_command(root, command)
    if not exe:
        return ""
    return _version_from_command(exe) or _file_identity(exe)


def _dependency_status(installed: str, latest: str) -> str:
    if not installed:
        return "missing"
    if latest and latest != installed:
        return "outdated"
    if latest:
        return "current"
    return "unknown"


def dependency_report(root: Path) -> dict:
    packages: list[dict] = []
    errors: list[str] = []
    requirements = _python_requirements(release_dir(root))
    script_packages = [str(item.get("package") or "") for item in SCRIPT_CLIS]
    pypi_latest = _lookup_many([name for name, _ in requirements] + script_packages, _latest_pypi_version)
    npm_latest = _lookup_many(
        [item["name"] for item in NPM_RUNTIME_PACKAGES] + [item["name"] for item in LOCAL_NPM_CLIS],
        _latest_npm_version,
    )

    for name, required in requirements:
        installed = _installed_python_version(name)
        latest, err = pypi_latest.get(name, ("", ""))
        if err:
            errors.append(f"pypi:{name}: {err}")
        packages.append(
            {
                "manager": "pip",
                "name": name,
                "required": required,
                "installed_version": installed,
                "latest_version": latest,
                "status": _dependency_status(installed, latest),
                "source": "requirements.txt",
            }
        )

    npm_versions, npm_err = _npm_global_versions()
    if npm_err:
        errors.append(f"npm: {npm_err}")
    for item in NPM_RUNTIME_PACKAGES:
        name = item["name"]
        installed = npm_versions.get(name) or _version_from_command(item["command"])
        latest, err = npm_latest.get(name, ("", ""))
        if err:
            errors.append(f"npm:{name}: {err}")
        packages.append(
            {
                "manager": "npm",
                "name": name,
                "required": "global runtime",
                "installed_version": installed,
                "latest_version": latest,
                "status": _dependency_status(installed, latest),
                "source": "npm global",
                "command": item["command"],
            }
        )

    for item in LOCAL_NPM_CLIS:
        name = item["name"]
        exe = ""
        installed = ""
        for command in item["commands"]:
            exe = _resolve_command(root, command)
            if exe:
                installed = _version_from_command(exe) or _file_identity(exe)
                break
        latest, err = npm_latest.get(name, ("", ""))
        if err:
            errors.append(f"npm:{name}: {err}")
        packages.append(
            {
                "manager": "npm",
                "name": item["label"],
                "package": name,
                "required": "local runtime CLI",
                "installed_version": installed,
                "latest_version": latest,
                "status": _dependency_status(installed, latest),
                "source": ".silicon-interface",
                "command": exe or item["commands"][0],
            }
        )

    for item in SCRIPT_CLIS:
        name = item["name"]
        installed = ""
        package = str(item.get("package") or "")
        target = str(item.get("target_version") or "")
        if package:
            installed = _python_console_package_version(root, item["command"], package)
        installed = installed or _command_identity(root, item["command"])
        if target:
            latest, err = target, ""
        elif package:
            latest, err = pypi_latest.get(package, ("", ""))
        else:
            latest, err = _latest_github_main(item["latest_repo"])
        if err:
            label = f"pypi:{package}" if package else f"github:{item['latest_repo']}"
            errors.append(f"{label}: {err}")
        if not installed:
            status = "missing"
        elif installed.startswith("sha256:"):
            status = "unknown"
        else:
            status = _dependency_status(installed, latest)
        packages.append(
            {
                "manager": "script",
                "name": name,
                "package": package,
                "required": item["source"],
                "installed_version": installed,
                "latest_version": latest,
                "status": status,
                "source": item["source"],
                "command": item["command"],
            }
        )

    summary = {"total": len(packages), "current": 0, "outdated": 0, "missing": 0, "unknown": 0}
    for pkg in packages:
        summary[pkg["status"]] = summary.get(pkg["status"], 0) + 1

    return {
        "checked_at": now_iso(),
        "packages": packages,
        "summary": summary,
        "errors": errors[:20],
    }


def dependency_summary_text(report: dict, *, updated: bool = False) -> str:
    summary = report.get("summary") or {}
    total = int(summary.get("total") or 0)
    outdated = int(summary.get("outdated") or 0)
    missing = int(summary.get("missing") or 0)
    failed = int(summary.get("failed_installs") or 0)
    prefix = "dependency update" if updated else "dependency report"
    detail = f"{prefix}: {total} checked, {outdated} outdated, {missing} missing"
    if failed:
        detail += f", {failed} install step(s) failed"
    return detail


def terminal_frame(ws, **payload) -> None:
    send_json(ws, {"type": "terminal", **payload})


def _terminal_reader(ws, session_id: str, provider: str, fd: int, proc: subprocess.Popen) -> None:
    try:
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            terminal_frame(
                ws,
                event="output",
                provider=provider,
                session_id=session_id,
                data=chunk.decode("utf-8", errors="replace"),
            )
    finally:
        rc = proc.poll()
        if rc is None:
            try:
                rc = proc.wait(timeout=1)
            except Exception:
                rc = None
        with TERMINAL_LOCK:
            current = TERMINAL_SESSION.get("id") == session_id
            maintenance_activity = (
                dict(TERMINAL_SESSION.get("maintenance_activity") or {})
                if current
                else {}
            )
            if current:
                TERMINAL_SESSION.clear()
        try:
            os.close(fd)
        except OSError:
            pass
        if current:
            try:
                from core.maintenance import MaintenanceCoordinator

                lease_id = str(maintenance_activity.get("lease_id") or "")
                if lease_id:
                    MaintenanceCoordinator(silicon_dir()).release(lease_id)
            except Exception:
                pass
            terminal_frame(
                ws,
                event="exit",
                provider=provider,
                session_id=session_id,
                returncode=rc,
            )


def terminal_stop(ws=None, reason: str = "stopped") -> bool:
    with TERMINAL_LOCK:
        session = dict(TERMINAL_SESSION)
        TERMINAL_SESSION.clear()
    if not session:
        return False

    proc = session.get("proc")
    if isinstance(proc, subprocess.Popen) and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    fd = session.get("fd")
    if isinstance(fd, int):
        try:
            os.close(fd)
        except OSError:
            pass

    reference = dict(session.get("maintenance_activity") or {})
    if isinstance(proc, subprocess.Popen) and reference:
        def release_when_stopped():
            try:
                from core.maintenance import MaintenanceCoordinator

                coordinator = MaintenanceCoordinator(silicon_dir())
                lease_id = str(reference.get("lease_id") or "")
                while proc.poll() is None:
                    if lease_id:
                        coordinator.heartbeat(lease_id)
                    time.sleep(1)
                if lease_id:
                    coordinator.release(lease_id)
            except Exception:
                pass

        threading.Thread(
            target=release_when_stopped,
            name="glass-terminal-stop-lease",
            daemon=True,
        ).start()

    if ws is not None:
        terminal_frame(
            ws,
            event="stopped",
            provider=str(session.get("provider") or ""),
            session_id=str(session.get("id") or ""),
            reason=reason,
        )
    return True


def terminal_start(ws, root: Path, provider: str) -> None:
    provider = (provider or "").strip().lower()
    args = TERMINAL_COMMANDS.get(provider)
    if not args:
        terminal_frame(ws, event="error", provider=provider, message="unknown terminal provider")
        return

    exe = shutil.which(args[0])
    if not exe:
        terminal_frame(ws, event="error", provider=provider, message=f"{args[0]} not found")
        return

    terminal_stop(ws, reason="replaced")
    if pty is None:
        terminal_frame(
            ws,
            event="error",
            provider=provider,
            message="interactive Glass terminals are not supported on this platform",
        )
        return
    try:
        from core.maintenance import MaintenanceCoordinator

        coordinator = MaintenanceCoordinator(root)
        maintenance_activity = coordinator.acquire_activity(
            "glass_terminal",
            activity_id=f"interactive-{provider}",
        )
    except Exception:
        maintenance_activity = None
        coordinator = None
    if maintenance_activity is None:
        terminal_frame(
            ws,
            event="error",
            provider=provider,
            message="Silicon is preparing an update; new terminal sessions are paused.",
        )
        return
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
        if coordinator is not None:
            coordinator.release(maintenance_activity)
        terminal_frame(ws, event="error", provider=provider, message=str(exc))
        return

    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    cmd = [exe, *args[1:]]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except Exception as exc:
        if coordinator is not None:
            coordinator.release(maintenance_activity)
        try:
            os.close(master_fd)
            os.close(slave_fd)
        except OSError:
            pass
        terminal_frame(ws, event="error", provider=provider, message=str(exc))
        return
    try:
        os.close(slave_fd)
    except OSError:
        pass

    session_id = secrets.token_hex(8)
    with TERMINAL_LOCK:
        TERMINAL_SESSION.update(
            {
                "id": session_id,
                "provider": provider,
                "proc": proc,
                "fd": master_fd,
                "maintenance_activity": maintenance_activity.reference(),
            }
        )
    terminal_frame(
        ws,
        event="started",
        provider=provider,
        session_id=session_id,
        command=" ".join(args),
    )
    thread = threading.Thread(
        target=_terminal_reader,
        args=(ws, session_id, provider, master_fd, proc),
        daemon=True,
    )
    thread.start()
    def heartbeat_terminal():
        while proc.poll() is None:
            with TERMINAL_LOCK:
                if TERMINAL_SESSION.get("id") != session_id:
                    return
            if coordinator is None or not coordinator.heartbeat(
                maintenance_activity
            ):
                return
            time.sleep(20)

    threading.Thread(
        target=heartbeat_terminal,
        name=f"glass-terminal-lease-{session_id}",
        daemon=True,
    ).start()


def terminal_input(ws, data: str) -> None:
    with TERMINAL_LOCK:
        session = dict(TERMINAL_SESSION)
    fd = session.get("fd")
    if not isinstance(fd, int):
        terminal_frame(ws, event="error", message="no active terminal session")
        return
    try:
        os.write(fd, str(data or "")[:4000].encode("utf-8", errors="replace"))
    except OSError as exc:
        terminal_frame(
            ws,
            event="error",
            provider=str(session.get("provider") or ""),
            session_id=str(session.get("id") or ""),
            message=str(exc),
        )


def handle_terminal_message(ws, msg: dict, root: Path) -> None:
    action = (msg.get("action") or "").strip().lower()
    if action == "start":
        terminal_start(ws, root, str(msg.get("provider") or ""))
    elif action == "input":
        terminal_input(ws, str(msg.get("data") or ""))
    elif action == "stop":
        if not terminal_stop(ws):
            terminal_frame(ws, event="status", message="no active terminal session")
    else:
        terminal_frame(ws, event="error", message="unknown terminal action")


def _spawn_silicon_cli(root: Path, action: str, *, delay: float = 0) -> None:
    """Run the platform CLI from this instance without shell-injected targets."""

    if action not in {"start", "stop", "restart"}:
        raise ValueError("Unsupported Silicon lifecycle action.")
    child = (
        "import os,shutil,subprocess,sys,time;"
        "time.sleep(float(sys.argv[1]));"
        "cli=shutil.which('silicon') or 'silicon';"
        "cmd=([os.environ.get('COMSPEC','cmd.exe'),'/d','/s','/c',cli,sys.argv[3]]"
        " if os.name=='nt' and cli.lower().endswith(('.cmd','.bat'))"
        " else [cli,sys.argv[3]]);"
        "raise SystemExit(subprocess.call(cmd,cwd=sys.argv[2],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))"
    )
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            child,
            str(max(0, delay)),
            str(root),
            action,
        ],
        **kwargs,
    )


def execute_command(command: dict, root: Path, name: str) -> tuple[str, str]:
    action = command.get("command", "")
    if action in {"backup", "backup_now"}:
        return run_backup(root, note=f"glass command {command.get('id') or ''}".strip())
    if action == "start":
        try:
            _spawn_silicon_cli(root, "start")
            return "done", "started"
        except Exception as exc:
            return "failed", str(exc)
    if action == "stop":
        try:
            _spawn_silicon_cli(root, "stop")
            return "done", "stopped"
        except Exception as exc:
            return "failed", str(exc)
    if action == "version":
        # Report the version this silicon is currently running (silicon.info).
        return "done", local_version(root) or "unversioned"
    if action in {"dependencies", "dependency_report"}:
        report = dependency_report(root)
        command["_status_patch"] = {
            "dependencies": report,
            "dependency_check_at": report.get("checked_at"),
        }
        return "done", dependency_summary_text(report)
    if action in {"dependency_update", "dependencies_update"}:
        report = dependency_report(root)
        command["_status_patch"] = {
            "dependencies": report,
            "dependency_check_at": report.get("checked_at"),
        }
        return (
            "failed",
            "in-process dependency mutation is disabled; run "
            "`silicon update <name>` from the host so silicon-cli drains the "
            "Silicon and hydrates dependencies transactionally",
        )
    if action in {"fetch_latest", "update_check", "update", "git_update"}:
        # The running instance may check release status, but source mutation is
        # owned exclusively by the host silicon-cli transactional updater.
        try:
            from update import trigger_system_update_check

            result = trigger_system_update_check(force=True)
        except Exception as exc:
            return "failed", f"update check failed: {exc}"

        if result.get("status") == "error":
            return "failed", str(result.get("error") or "update check failed")
        local = str(result.get("local_version") or "unversioned")
        latest = str(result.get("latest_version") or "")
        if not result.get("update_available"):
            return "done", f"already on {local}"
        detail = (
            f"update {local} → {latest} is available; run "
            "`silicon update <name>` from the host (it drains, stops, and "
            "restarts the Silicon safely)"
        )
        if action in {"fetch_latest", "update_check"}:
            return "done", detail
        return "failed", detail
    return "failed", f"unknown command: {action}"


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


def handle_message(
    ws,
    msg: dict,
    root: Path,
    name: str,
    config: dict | None = None,
    team_context_reconciler: TeamContextReconciler | None = None,
    trust_policy_reconciler: TrustPolicyReconciler | None = None,
) -> None:
    msg_type = msg.get("type")
    if msg_type == "welcome":
        print("[glass-agent] welcome", flush=True)
        return
    if msg_type == "billing":
        print(f"[glass-agent] billing: {msg.get('status') or msg.get('message') or 'ok'}", flush=True)
        return
    if msg_type == "pong":
        return
    if msg_type == "team_context.changed":
        _request_team_context_reconcile(
            team_context_reconciler,
            reason=_team_context_change_reason(msg),
        )
        return
    if msg_type == "trust.changed":
        _request_trust_reconcile(
            trust_policy_reconciler,
            reason="websocket-invalidation:trust",
        )
        return
    if msg_type == "diag.rollup.ack":
        try:
            from core.glass_diag_push import acknowledge, resolve_db_path

            settings = config or {}
            db_path = resolve_db_path(root, settings.get("diag_db"))
            stored = bool(msg.get("stored"))
            acknowledge(
                db_path,
                msg.get("run_id", ""),
                stored=stored,
                reason=msg.get("reason", ""),
            )
            if not stored:
                rejection = str(msg.get("reason") or "invalid rollup")[:300]
                print(
                    f"[glass-agent] diagnostic rejected run_id={msg.get('run_id', '')}: "
                    f"{rejection}",
                    flush=True,
                )
                send_json(ws, {
                    "type": "log",
                    "level": "error",
                    "source": "diagnostics",
                    "msg": (
                        f"Diagnostic rollup rejected for run "
                        f"{str(msg.get('run_id') or '')[:64]}: {rejection}"
                    ),
                })
        except Exception as exc:
            print(f"[glass-agent] diagnostic ack deferred: {exc}", flush=True)
        return
    if msg_type == "terminal":
        handle_terminal_message(ws, msg, root)
        return
    if msg_type != "command":
        return

    command_id = msg.get("id", "")
    if command_id:
        send_json(ws, {"type": "command_ack", "id": command_id, "command": msg.get("command", "")})
    status, detail = execute_command(msg, root, name)
    # Keep Glass's stored status fresh — the console reads `version` from it
    # (the on-demand "version" command, and any command that may change it).
    status_update = {"type": "status", "version": local_version(root)}
    patch = msg.pop("_status_patch", {})
    if isinstance(patch, dict):
        status_update.update(patch)
    send_json(ws, status_update)
    if command_id:
        send_json(ws, {
            "type": "command_result",
            "id": command_id,
            "command": msg.get("command", ""),
            "status": status,
            "message": detail,
        })
    print(f"[glass-agent] command {msg.get('command')} -> {status}: {detail}", flush=True)
    if msg.pop("_agent_reexec", False):
        print("[glass-agent] re-execing to load updated code", flush=True)
        time.sleep(1)
        os.execv(sys.executable, [sys.executable, "-u", str(Path(__file__).resolve())])


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
    key = glass_api_key(config)
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
            print("[glass-agent] connected", flush=True)
            if on_connected is not None:
                on_connected()
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
            send_json(ws, status_payload(root))
            drain_diagnostics(ws, root, config)
            now = time.monotonic()
            next_status = now + STATUS_INTERVAL
            next_ping = now + PING_INTERVAL
            next_diagnostics = now + DIAGNOSTICS_INTERVAL
            next_team_context = now + TEAM_CONTEXT_INTERVAL
            next_trust_policy = now + TRUST_POLICY_INTERVAL

            while running[0]:
                log_tailer.poll(lambda frame: send_json(ws, frame))
                now = time.monotonic()
                if now >= next_status:
                    send_json(ws, status_payload(root))
                    next_status = now + STATUS_INTERVAL
                if now >= next_ping:
                    send_json(ws, {"type": "ping", "ts": int(time.time())})
                    next_ping = now + PING_INTERVAL
                if now >= next_diagnostics:
                    drain_diagnostics(ws, root, config)
                    next_diagnostics = now + DIAGNOSTICS_INTERVAL
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

                try:
                    raw = ws.recv(timeout=1)
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
    if not config.get("server_url") or not glass_api_key(config):
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
            key = glass_api_key(config)
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
            connected = False

            def mark_connected():
                nonlocal backoff, connected
                connected = True
                backoff = 1

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
                    delay = AUTH_REJECTION_BACKOFF if rejected else backoff
                    reason = "authentication rejected" if rejected else str(exc)
                    print(f"[glass-agent] disconnected: {reason}; reconnecting in {delay}s", flush=True)
                    wait_for_retry(
                        root,
                        running,
                        delay,
                        key if rejected else "",
                        str(config.get("server_url") or "") if rejected else None,
                    )
                    if rejected:
                        backoff = 1
                    elif connected:
                        # A live connection broke, so the next attempt starts at
                        # the minimum delay rather than inheriting old failures.
                        backoff = 2
                    else:
                        backoff = min(backoff * 2, MAX_BACKOFF)
    finally:
        reconciler.stop()
        trust_reconciler.stop()
        pid_file.unlink(missing_ok=True)
    print("[glass-agent] stopped", flush=True)


if __name__ == "__main__":
    main()
