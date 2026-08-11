"""A live provider login shell, driven from the Glass console.

`claude` and `codex` both authenticate through an interactive terminal. This
opens one under a pseudo-terminal, streams it to the console, and guarantees
that only one session exists at a time — a second login racing the first is how
a half-written credential file happens.
"""
from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from interface.agent.config import silicon_dir
from interface.agent.frames import send_json

try:
    import pty
except ImportError:  # Windows has no pseudo-terminal module.
    pty = None

TERMINAL_COMMANDS = {
    "claude": ("claude",),
    "codex": ("codex", "login"),
}
TERMINAL_LOCK = threading.Lock()
TERMINAL_SESSION: dict[str, object] = {}


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
                from manager.runtime.maintenance import MaintenanceCoordinator

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
                from manager.runtime.maintenance import MaintenanceCoordinator

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
        from manager.runtime.maintenance import MaintenanceCoordinator

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


