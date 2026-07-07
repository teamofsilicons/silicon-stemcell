#!/usr/bin/env python3
"""Glass sidecar for Silicon v1.

Keeps one live connection to Glass control, reports status, and runs manifest
backups when Glass asks for them.
"""
from __future__ import annotations

import hashlib
import json
import os
import pty
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
from urllib.parse import quote
from urllib.request import Request, urlopen

STATUS_INTERVAL = 15
PING_INTERVAL = 20
MAX_BACKOFF = 30
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


def silicon_dir() -> Path:
    return Path(__file__).resolve().parent


def local_bin_dir(root: Path) -> Path:
    return root / ".local" / "bin"


def prepend_local_bin(root: Path) -> None:
    bin_dir = str(local_bin_dir(root))
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in parts:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


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
    with SEND_LOCK:
        ws.send(json.dumps(payload, separators=(",", ":")))


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
    try:
        from core.backup import run_backup as manifest_backup

        ok = manifest_backup(root, note=note, logger=lambda msg: print(f"[glass-agent] {msg}", flush=True))
        return ("done", "backup complete") if ok else ("failed", "backup skipped")
    except Exception as exc:
        return "failed", str(exc)


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
    requirements = _python_requirements(root)
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


def _trim(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _run_install(cmd: list[str], root: Path, timeout: int = 900) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        detail = _trim(proc.stderr or proc.stdout or "")
        return {
            "command": " ".join(cmd),
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "detail": detail,
        }
    except Exception as exc:
        return {"command": " ".join(cmd), "ok": False, "returncode": None, "detail": str(exc)}


def _update_python_cli(root: Path, item: dict) -> dict:
    command = str(item["command"])
    package = str(item["package"])
    target = str(item.get("target_version") or "").strip()
    requirement = f"{package}=={target}" if target else package
    exe = _resolve_command(root, command)
    if not exe:
        return {
            "command": f"{command}: upgrade Python package {requirement}",
            "ok": False,
            "returncode": None,
            "detail": f"{command} not found",
        }
    runner = _python_runner_from_executable(exe)
    if not runner:
        return {
            "command": f"{command}: upgrade Python package {package}",
            "ok": False,
            "returncode": None,
            "detail": f"{command} is not a Python-backed CLI",
        }
    result = _run_install([*runner, "-m", "pip", "install", "--upgrade", requirement], root, timeout=1200)
    if result.get("ok"):
        return result

    # Some fleet images have Python CLIs in a root-owned /opt runtime. Fall back
    # to a per-silicon venv and put its console script first on PATH.
    tool_root = root / ".tools" / command
    bin_dir = local_bin_dir(root)
    venv_python = tool_root / "bin" / "python"
    try:
        tool_root.parent.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        if not venv_python.exists():
            create = subprocess.run(
                [sys.executable, "-m", "venv", str(tool_root)],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if create.returncode != 0:
                detail = _trim(create.stderr or create.stdout or "")
                result["detail"] = f"{result.get('detail') or ''}\nlocal venv create failed: {detail}".strip()
                return result
        fallback = _run_install(
            [str(venv_python), "-m", "pip", "install", "--upgrade", requirement],
            root,
            timeout=1200,
        )
        script = tool_root / "bin" / command
        target = bin_dir / command
        if fallback.get("ok") and script.exists():
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(script)
            prepend_local_bin(root)
            fallback["command"] = f"{fallback['command']} && link {target}"
        return fallback
    except Exception as exc:
        result["detail"] = f"{result.get('detail') or ''}\nlocal install failed: {exc}".strip()
        return result


def update_dependencies(root: Path) -> dict:
    install_results: list[dict] = []
    req = root / "requirements.txt"
    if req.exists():
        attempts = [
            [sys.executable, "-m", "pip", "install", "--upgrade", "-r", str(req)],
            [sys.executable, "-m", "pip", "install", "--upgrade", "--user", "-r", str(req)],
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--break-system-packages",
                "-r",
                str(req),
            ],
        ]
        for cmd in attempts:
            result = _run_install(cmd, root)
            install_results.append(result)
            if result["ok"]:
                break

    npm = shutil.which("npm")
    if npm:
        install_results.append(
            _run_install(
                [npm, "install", "-g", *(item["name"] for item in NPM_RUNTIME_PACKAGES)],
                root,
                timeout=1200,
            )
        )
    else:
        install_results.append(
            {
                "command": "npm install -g " + " ".join(item["name"] for item in NPM_RUNTIME_PACKAGES),
                "ok": False,
                "returncode": None,
                "detail": "npm not found",
            }
        )

    for item in LOCAL_NPM_CLIS:
        if npm:
            install_results.append(
                _run_install(
                    [
                        npm,
                        "exec",
                        "--yes",
                        "--package",
                        item["name"],
                        "--",
                        item["install_command"],
                        "install",
                        str(root),
                    ],
                    root,
                    timeout=1200,
                )
            )
        else:
            install_results.append(
                {
                    "command": f"npm exec --package {item['name']} -- {item['install_command']} install {root}",
                    "ok": False,
                    "returncode": None,
                    "detail": "npm not found",
                }
            )

    for item in SCRIPT_CLIS:
        if item.get("update_kind") == "python_cli":
            install_results.append(_update_python_cli(root, item))
            continue
        exe = _resolve_command(root, item["command"])
        if exe:
            install_results.append(
                _run_install([exe, *item["update_args"]], root, timeout=900)
            )
        else:
            install_results.append(
                {
                    "command": " ".join((item["command"], *item["update_args"])),
                    "ok": False,
                    "returncode": None,
                    "detail": f"{item['command']} not found",
                }
            )

    report = dependency_report(root)
    report["updated_at"] = now_iso()
    report["install_results"] = install_results
    report["summary"]["failed_installs"] = sum(1 for r in install_results if not r.get("ok"))
    return report


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
            if current:
                TERMINAL_SESSION.clear()
        try:
            os.close(fd)
        except OSError:
            pass
        if current:
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
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
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
            {"id": session_id, "provider": provider, "proc": proc, "fd": master_fd}
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
    if action in {"dependencies", "dependency_report"}:
        report = dependency_report(root)
        command["_status_patch"] = {
            "dependencies": report,
            "dependency_check_at": report.get("checked_at"),
        }
        return "done", dependency_summary_text(report)
    if action in {"dependency_update", "dependencies_update"}:
        report = update_dependencies(root)
        command["_status_patch"] = {
            "dependencies": report,
            "dependency_check_at": report.get("checked_at"),
            "dependency_update_at": report.get("updated_at"),
        }
        failed = int((report.get("summary") or {}).get("failed_installs") or 0)
        status = "failed" if failed else "done"
        return status, dependency_summary_text(report, updated=True)
    if action in {"fetch_latest", "update_check", "update", "git_update"}:
        # Git-based, pull-only update: merge upstream while preserving the
        # silicon's own code + living data, then restart to load it. The version
        # rides in via the merged silicon.info; the status frame sent right after
        # this command reports it to Glass (which is what the rollout polls).
        try:
            proc = subprocess.run(
                [
                    sys.executable, "-c",
                    "import json,sys; sys.path.insert(0, '.'); "
                    "from core.git_update import git_apply; print(json.dumps(git_apply()))",
                ],
                cwd=str(root), capture_output=True, text=True, timeout=1800,
            )
            lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
            result = json.loads(lines[-1]) if lines else {}
        except Exception as exc:
            return "failed", f"git update error: {exc}"

        st = result.get("status")
        if st == "up_to_date":
            return "done", f"already on {result.get('version')}"
        if st == "updated":
            # Restart silicon + agent to load the new code. Delay a few seconds
            # so this command's status/result frames reach Glass before the stop
            # tears us down. Pass `name` as $1 so it can't be shell-injected.
            command["_agent_reexec"] = True
            try:
                subprocess.Popen(
                    ["sh", "-c", 'sleep 3; silicon restart "$1"', "_", name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                )
            except Exception as exc:
                return "done", f"updated to {result.get('version')} (restart failed: {exc})"
            mode = result.get("mode")
            return "done", f"updated to {result.get('version')}{f' ({mode})' if mode else ''}; restarting"
        return "failed", result.get("detail") or "git update failed"
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


def run_live(root: Path, config: dict, running: list[bool]) -> None:
    from websockets.sync.client import connect

    name = silicon_name(root)
    url = ws_url(config["server_url"], config["api_key"])
    print(f"[glass-agent] connecting to {config['server_url'].rstrip('/')}/ws/glass/agent/", flush=True)
    try:
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
    finally:
        terminal_stop()


def main() -> None:
    root = silicon_dir()
    prepend_local_bin(root)
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
