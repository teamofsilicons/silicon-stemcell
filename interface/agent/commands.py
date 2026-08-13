"""What Glass can ask this Silicon to do, and how it answers.

Status, backup, dependency report, start/stop/restart. Lifecycle actions go
through the platform CLI rather than being performed here, so one code path
starts a Silicon whoever asked.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from interface.agent.config import detect_status, local_version
from interface.agent import dependencies as dependencies_module

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
        from silicon.runtime.maintenance import (
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
        from interface.backup import run_backup as manifest_backup

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
        report = dependencies_module.dependency_report(root)
        command["_status_patch"] = {
            "dependencies": report,
            "dependency_check_at": report.get("checked_at"),
        }
        return "done", dependencies_module.dependency_summary_text(report)
    if action in {"dependency_update", "dependencies_update"}:
        report = dependencies_module.dependency_report(root)
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
            from interface.release.updater import trigger_system_update_check

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


