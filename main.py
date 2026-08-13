"""Silicon Stemcell process entrypoint.

Boot the runtime, then run the loop. What a manager does with a turn is in
``manager/``; what Silicon says to the outside world is in ``interface/``; how
it thinks is in ``inference/``.

Import order matters at the top of this file: the active release must be on
``sys.path`` and ``PATH`` must prefer the selected generation's bin directory
before anything else is imported, so a stale launcher cannot shadow it.
"""
from interface import outbound
import atexit
import hashlib
import time
import sys
import os
import signal
import subprocess

# Ensure the active immutable release is importable before resolving the
# durable instance root used by side-by-side updates.
CODE_ROOT = os.path.dirname(os.path.abspath(__file__))
if CODE_ROOT not in sys.path:
    sys.path.insert(0, CODE_ROOT)
from helpers.paths import DATA_ROOT
from helpers.silicon import SILICON

PROJECT_ROOT = os.fspath(DATA_ROOT)
LOCAL_BIN = os.path.join(PROJECT_ROOT, ".local", "bin")
ACTIVE_ENV_BIN = os.path.dirname(os.path.abspath(sys.executable))
_path_entries = [
    entry
    for entry in os.environ.get("PATH", "").split(os.pathsep)
    if entry and entry not in {ACTIVE_ENV_BIN, LOCAL_BIN}
]
# Package entry points belong to the selected generation environment. Keep that
# bin directory ahead of the legacy data-root bin so an old generated launcher
# can never shadow an installed command.
os.environ["PATH"] = os.pathsep.join(
    [ACTIVE_ENV_BIN, LOCAL_BIN, *_path_entries]
)

from diagnostics.logs import announce_session, runtime_log as log
from diagnostics.store import Diagnostics
from interface import (
    runtime_file_notifications_active,
    start_listener,
    start_runtime_file_watch,
    stop_listener,
    stop_runtime_file_watch,
    validate_contacts_integrity,
    wait_for_runtime_activity,
)
from interface.cron import CRON_INVALIDATION_FILE
from interface.long_tasks import (
    LONG_TASK_STATE_FILE,
    backfill_active_estimated_task_lifecycles,
    recover_long_task_lifecycles,
)
from interface.messages import MANAGER_MESSAGES_FILE
from interface.work import (
    WORK_UPDATES_FILE,
    complete_inactive_calls,
    next_inactive_call_deadline,
)
from manager.activity import _contact_has_active_workers
from manager.dispatcher import (
    ManagerDispatcher,
    _maintenance_runtime_tick,
    _merge_due_internal_roots,
)
from manager.loop import EventLoopSchedule, run_event_loop_tick
from manager.loop_config import EVENT_LOOP, LOOP_TICK
from manager.runtime.health import start_runtime_health, stop_runtime_health
from manager.runtime.maintenance import COORDINATOR as MAINTENANCE
from manager.runtime.restart import _check_restart_flag, _check_restart_request
from manager.settings import PROJECT_ROOT
from worker import get_worker_status
from worker.constants import ACTIVE_FILE, BROWSER_QUEUE_FILE


def _install_diagnostic_shutdown_hooks():
    """Persist in-flight evidence on normal exit and catchable termination."""
    atexit.register(
        Diagnostics.close_active_runs,
        reason="Silicon process exited before the run completed",
        category="process_exit",
    )

    def shutdown(signum, _frame):
        try:
            signal_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            signal_name = str(signum)
        Diagnostics.close_active_runs(
            reason=f"Silicon process received {signal_name}",
            category="process_signal",
        )
        raise SystemExit(128 + int(signum))

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, shutdown)


def _announce_session():
    """Record where this process starts in every log that will grow from it."""
    from inference import brain_order

    session_id = f"{int(time.time())}-{os.getpid()}"
    announce_session(
        session_id=session_id,
        provider=",".join(brain_order()),
        version=_local_version(),
    )
    return session_id


def _local_version() -> str:
    try:
        from interface.release.updater import _local_version as read_version

        return read_version()
    except Exception:
        return ""


def _bootstrap_team_context():
    """Best-effort team mirror before any manager can receive a startup turn."""
    try:
        from interface.team import reconcile_team_context

        return reconcile_team_context(
            PROJECT_ROOT,
            force=True,
            reason="startup",
        )
    except Exception as exc:
        log(f"[Silicon] Team context startup sync skipped: {exc}")
        return None


def _bootstrap_trust_policy():
    """Best-effort Glass trust sync before any manager can receive a turn."""
    try:
        from interface.trust import reconcile_trust_policy

        result = reconcile_trust_policy(
            PROJECT_ROOT,
            force=True,
            reason="startup",
        )
        if result.get("status") == "deferred":
            log(
                "[Silicon] Trust policy startup sync deferred; managers will "
                "fail closed at very_low until Glass confirms it."
            )
        return result
    except Exception as exc:
        log(f"[Silicon] Trust policy startup sync skipped: {exc}")
        return None


def main():
    _install_diagnostic_shutdown_hooks()
    _announce_session()
    log("[Silicon] Starting event loop...")
    log(
        "[Silicon] Event-driven scheduler active; independent recovery "
        f"checks are capped at {LOOP_TICK}s for interactive sources"
    )

    _bootstrap_team_context()
    _bootstrap_trust_policy()
    start_listener()
    start_runtime_file_watch(
        [
            MAINTENANCE.state_file,
            MANAGER_MESSAGES_FILE,
            WORK_UPDATES_FILE,
            LONG_TASK_STATE_FILE,
            ACTIVE_FILE,
            BROWSER_QUEUE_FILE,
            CRON_INVALIDATION_FILE,
        ]
    )
    recovered_long_tasks = recover_long_task_lifecycles(
        reply_sender=outbound.reply_contact,
        has_active_workers=_contact_has_active_workers,
        worker_status_resolver=get_worker_status,
    )
    if recovered_long_tasks:
        log(
            "[Silicon] Replaying "
            f"{recovered_long_tasks} durable long-task lifecycle(s)."
        )
    backfilled_long_tasks = backfill_active_estimated_task_lifecycles(
        reply_sender=outbound.reply_contact,
        has_active_workers=_contact_has_active_workers,
        worker_status_resolver=get_worker_status,
    )
    if backfilled_long_tasks:
        log(
            "[Silicon] Backfilled "
            f"{backfilled_long_tasks} active estimated task lifecycle(s)."
        )
    dispatcher = ManagerDispatcher()
    start_runtime_health(
        lambda: str(MAINTENANCE.public_status().get("phase", "available"))
    )

    # Check if we just restarted. There is one session to tell, so the carbon
    # the restart was requested for is context rather than a routing decision.
    restart_msg, restart_carbon_id = _check_restart_flag()
    if restart_msg:
        log(f"[Silicon] Post-restart ({restart_carbon_id or 'no contact'}): {restart_msg}")
        dispatcher.submit({SILICON: restart_msg})

    schedule = EventLoopSchedule(EVENT_LOOP, identity=PROJECT_ROOT)
    activity_ready = True
    next_contact_integrity = 0.0
    contact_jitter = int.from_bytes(
        hashlib.sha256(f"{PROJECT_ROOT}:contacts".encode("utf-8")).digest()[:2],
        "big",
    ) % 61
    failure_count = 0
    failure_not_before = 0.0
    try:
        while True:
            now = time.monotonic()
            if now < failure_not_before:
                # A global scheduler failure should never turn into a hot
                # retry loop, even if a burst of pending wakeups is queued.
                time.sleep(failure_not_before - now)
                activity_ready = True
            maintenance_active = False
            eligible_handlers = set(schedule.handlers)
            try:
                maintenance_status = _maintenance_runtime_tick(
                    dispatcher,
                    attest=False,
                )
                maintenance_active = (
                    maintenance_status.get("phase") != "available"
                )
                now = time.monotonic()
                if not maintenance_active:
                    _check_restart_request()
                if not maintenance_active and now >= next_contact_integrity:
                    validate_contacts_integrity()
                    next_contact_integrity = now + 300.0 + contact_jitter

                if maintenance_active:
                    eligible_handlers = {
                        "check_interface",
                        "check_manager_messages",
                        "check_workers",
                    }
                else:
                    eligible_handlers = set(schedule.handlers)
                selected_handlers = schedule.due(
                    now,
                    activity=activity_ready,
                    eligible=eligible_handlers,
                )
                activity_ready = False
                context_by_carbon = _merge_due_internal_roots(
                    run_event_loop_tick(selected_handlers),
                    maintenance_active=maintenance_active,
                )
                schedule.record_attempts(selected_handlers, now)
                call_deadline = next_inactive_call_deadline()
                if (
                    call_deadline is not None
                    and call_deadline <= time.time()
                ):
                    try:
                        complete_inactive_calls()
                    except Exception as exc:
                        log(
                            "[Work updates] inactive call completion "
                            f"deferred: {exc}"
                        )
                if context_by_carbon:
                    for cid, ctx in context_by_carbon.items():
                        log(f"[Silicon] Context for {cid}:\n{ctx[:200]}...")
                    dispatcher.submit(context_by_carbon)
                _maintenance_runtime_tick(dispatcher, attest=True)
                failure_count = 0
                failure_not_before = 0.0
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log(f"[Silicon] Error: {exc}")
                failure_count += 1
                failure_not_before = time.monotonic() + min(
                    30.0,
                    float(2 ** min(failure_count - 1, 5)),
                )
            now = time.monotonic()
            timeout = schedule.seconds_until_due(
                now,
                eligible=eligible_handlers,
            )
            if not maintenance_active:
                timeout = min(
                    timeout,
                    max(0.0, next_contact_integrity - now),
                )
            call_deadline = next_inactive_call_deadline()
            if call_deadline is not None:
                timeout = min(
                    timeout,
                    max(0.0, call_deadline - time.time()),
                )
            # Native notifications cover every cross-process runtime source.
            # The minute cap is recovery-only; platforms without a healthy
            # native watcher retain the original half-second polling safety.
            fallback = 60.0 if runtime_file_notifications_active() else 0.5
            activity_ready = wait_for_runtime_activity(min(timeout, fallback))
    except KeyboardInterrupt:
        log("\n[Silicon] Shutting down.")
        raise SystemExit(0)
    finally:
        stop_runtime_health()
        stop_runtime_file_watch()
        stop_listener()
        dispatcher.shutdown(wait=False)


def run_headed_browser():
    """Open headed browser via silicon-browser for manual login.
    silicon-browser has built-in stealth and bundles its own browser."""
    from worker import SILICON_BROWSER_PROFILE

    log("[Silicon] Opening headed browser for login")
    log(f"[Silicon] Profile: {SILICON_BROWSER_PROFILE}")
    log("[Silicon] Log into any services you need. Press Ctrl+C when done.")
    log("")

    cmd = [
        "silicon-browser",
        "--profile", SILICON_BROWSER_PROFILE,
        "--headed",
        "open", "https://google.com",
    ]

    try:
        subprocess.run(cmd)
        log("[Silicon] Browser open. Log into your services, then press Ctrl+C to save and close.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("\n[Silicon] Closing browser and saving profile...")
        subprocess.run([
            "silicon-browser",
            "--profile", SILICON_BROWSER_PROFILE,
            "close",
        ], capture_output=True)
        log("[Silicon] Profile saved. Login state persisted for browser workers.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "browser":
        run_headed_browser()
    else:
        main()
