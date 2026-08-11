"""The browser worker, and the one profile it has to share.

Only one worker may hold the persistent browser profile at a time — two would
fight over the same cookies and the same live session — so a second profiled
request queues instead of launching. An incognito worker has no such
constraint: it gets a fresh session and no profile, and cleans up its own
daemon afterwards.

The check for an idle profile, the decision to queue, and the launch all happen
inside one cross-process transaction. Splitting them lets two managers both see
an idle profile.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

from helpers.state import file_lock
from worker import constants
from worker.base import Worker

from worker.leases import _maintenance_reference
from worker.process import (
    _get_worker_provider_order,
    _launch_with_provider_order,
    _launch_worker_process,
    _normalize_provider,
)
from worker.registry import (
    _get_worker_record,
    _load_active,
    _load_browser_queue,
    _save_browser_queue,
    _worker_launch_lock_path,
)


class BrowserWorker(Worker):
    worker_type = "browser"

    def start(self, task: str, *, resume: bool = False, incognito: bool = False) -> str:
        if incognito:
            return self._start_incognito(task, resume=resume)
        return self._start_profiled(task, resume=resume)

    def _start_incognito(self, task: str, *, resume: bool) -> str:
        with file_lock(_worker_launch_lock_path(self.worker_id)):
            if self.worker_id in _load_active():
                return f"Error: Worker '{self.worker_id}' is already active."
            record = _get_worker_record(self.worker_id)
            provider, session_id = self._resume_arguments(record)
            if resume:
                _, result = _launch_worker_process(
                    self.worker_id, task, "browser", self.carbon_id,
                    incognito=True, resume=True,
                    provider=provider or "claude", session_id=session_id,
                )
            else:
                _, result = _launch_with_provider_order(
                    self.worker_id, task, "browser", self.carbon_id, incognito=True
                )
            return result

    def _start_profiled(self, task: str, *, resume: bool) -> str:
        # One transaction: the idle check, the queue decision, and the launch.
        with file_lock(constants.PROFILED_BROWSER_LOCK_FILE), file_lock(constants.BROWSER_QUEUE_FILE):
            if self.worker_id in _load_active():
                return f"Error: Worker '{self.worker_id}' is already active."

            record = _get_worker_record(self.worker_id)
            provider, session_id = self._resume_arguments(record)

            queue = _load_browser_queue()
            if any(job["worker_id"] == self.worker_id for job in queue):
                return (
                    f"Error: Worker '{self.worker_id}' is already in the browser queue."
                )

            if not profiled_browser_active():
                if resume:
                    _, result = _launch_worker_process(
                        self.worker_id, task, "browser", self.carbon_id,
                        incognito=False, resume=True,
                        provider=provider or "claude", session_id=session_id,
                    )
                else:
                    _, result = _launch_with_provider_order(
                        self.worker_id, task, "browser", self.carbon_id, incognito=False
                    )
                return result

            providers = (
                [_normalize_provider(provider)]
                if resume and provider
                else _get_worker_provider_order("browser")
            )
            queue.append({
                "worker_id": self.worker_id,
                "task": task,
                "carbon_id": self.carbon_id,
                "queued_at": time.time(),
                "incognito": False,
                "resume": resume,
                "session_id": session_id,
                "provider": provider,
                "providers": providers,
                "maintenance_activity": _maintenance_reference(),
            })
            _save_browser_queue(queue)

            position = len(queue)
            action = "resume" if resume else "start"
            return (
                f"Done. Worker '{self.worker_id}' (browser) queued at position "
                f"{position}. Will {action} when current profiled browser worker "
                "finishes."
            )


def profiled_browser_active():
    active = _load_active()
    for info in active.values():
        if info.get("worker_type") == "browser" and not info.get("incognito", False):
            return True
    return False


def _get_silicon_browser_socket_dir():
    override = os.environ.get("SILICON_BROWSER_SOCKET_DIR")
    if override:
        return override
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "silicon-browser")
    home = os.path.expanduser("~")
    if home:
        return os.path.join(home, ".silicon-browser")
    return os.path.join("/tmp", "silicon-browser")


def _kill_incognito_daemon_by_pid(worker_id):
    socket_dir = _get_silicon_browser_socket_dir()
    pid_file = os.path.join(socket_dir, f"incognito-{worker_id}.pid")
    if not os.path.exists(pid_file):
        return
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    for ext in (".pid", ".sock", ".stream"):
        try:
            fpath = os.path.join(socket_dir, f"incognito-{worker_id}{ext}")
            if os.path.exists(fpath):
                os.unlink(fpath)
        except Exception:
            pass


def _cleanup_silicon_browser_session(worker_id, worker_info):
    if worker_info.get("worker_type") != "browser":
        return
    if worker_info.get("incognito", False):
        try:
            result = subprocess.run(
                ["silicon-browser", "--session", f"incognito-{worker_id}", "close"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                _kill_incognito_daemon_by_pid(worker_id)
        except Exception:
            _kill_incognito_daemon_by_pid(worker_id)


def sweep_orphaned_daemons():
    socket_dir = _get_silicon_browser_socket_dir()
    if not os.path.exists(socket_dir):
        return []

    active = _load_active()
    active_worker_ids = set(active.keys())
    cleaned = []

    for fname in os.listdir(socket_dir):
        if not fname.startswith("incognito-") or not fname.endswith(".pid"):
            continue
        worker_id = fname[len("incognito-"):-len(".pid")]
        if worker_id not in active_worker_ids:
            _kill_incognito_daemon_by_pid(worker_id)
            cleaned.append(worker_id)

    return cleaned
