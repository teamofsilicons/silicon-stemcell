import threading

from core.interface import get_unread_events
from core.cron import check_crons
from core.messages import check_manager_messages
from worker.handler import check_completed_workers_formatted, clean_old_archives
from update import check_for_system_update

LOOP_TICK = 10  # Time in seconds between each loop tick
ARCHIVE_FOR = (
    7 * 24 * 60 * 60
)  # Time in seconds to keep archived worker states (7 days)

_TEAM_CONTEXT_LOCK = threading.Lock()
_TEAM_CONTEXT_RUNNING = False
_TEAM_CONTEXT_PENDING_NOTICE = ""
_TEAM_CONTEXT_LAST_NOTICE = ""
_TEAM_CONTEXT_MAINTENANCE_ACTIVITY = None


def _team_context_notice(result):
    status = str((result or {}).get("own_status") or (result or {}).get("status") or "")
    if status == "conflict":
        return (
            "Your advertising-memory draft conflicts with a newer Glass revision. "
            "The local draft is preserved. Review it, then intentionally resolve "
            "with advertising_memory/update and resolve_conflict=true."
        )
    if status == "invalid":
        return (
            "Your local advertising memory is invalid and was not published. "
            "Replace it with advertising_memory/update; it must be valid UTF-8, "
            "at most 100 lines and 65,536 bytes, with no NUL characters."
        )
    if status == "pending":
        return (
            "Your advertising-memory draft is saved locally but has not reached "
            "Glass. Automatic retries will continue."
        )
    if status == "unauthorized":
        return (
            "Glass no longer authorizes this Silicon's team context. Stemcell "
            "has hidden the cached TEAM.md and peer advertising memories. Check "
            "the Silicon credential, active status, and team membership in Glass."
        )
    if status in {"state_error", "identity_changed"}:
        return (
            f"Advertising-memory synchronization needs attention ({status}). "
            "The runtime preserved local data and will retry safely."
        )
    return ""


def _run_team_context_tick():
    global _TEAM_CONTEXT_LAST_NOTICE
    global _TEAM_CONTEXT_PENDING_NOTICE
    global _TEAM_CONTEXT_RUNNING
    global _TEAM_CONTEXT_MAINTENANCE_ACTIVITY

    try:
        from core.team_context import team_context_tick
        from core.maintenance import heartbeat_scope

        activity = _TEAM_CONTEXT_MAINTENANCE_ACTIVITY
        if activity is not None:
            with heartbeat_scope([activity]):
                result = team_context_tick()
        else:
            result = team_context_tick()
        notice = _team_context_notice(result)
        with _TEAM_CONTEXT_LOCK:
            if notice and notice != _TEAM_CONTEXT_LAST_NOTICE:
                _TEAM_CONTEXT_PENDING_NOTICE = notice
                print(f"[Team Context] {notice}", flush=True)
            _TEAM_CONTEXT_LAST_NOTICE = notice
    except Exception as exc:
        print(f"[Team Context Error] {exc}", flush=True)
    finally:
        activity = _TEAM_CONTEXT_MAINTENANCE_ACTIVITY
        _TEAM_CONTEXT_MAINTENANCE_ACTIVITY = None
        if activity is not None:
            try:
                from core.maintenance import release_activity

                release_activity(activity)
            except Exception:
                pass
        with _TEAM_CONTEXT_LOCK:
            _TEAM_CONTEXT_RUNNING = False


def check_team_context():
    """Schedule a nonblocking sync and surface deduplicated actionable state."""
    global _TEAM_CONTEXT_PENDING_NOTICE
    global _TEAM_CONTEXT_RUNNING
    global _TEAM_CONTEXT_MAINTENANCE_ACTIVITY

    with _TEAM_CONTEXT_LOCK:
        notice = _TEAM_CONTEXT_PENDING_NOTICE
        if not _TEAM_CONTEXT_RUNNING:
            try:
                from core.maintenance import acquire_descendant_activity

                activity = acquire_descendant_activity(
                    "team_context_sync",
                    activity_id="periodic-team-context",
                )
            except Exception:
                activity = None
            if activity is not None:
                _TEAM_CONTEXT_MAINTENANCE_ACTIVITY = activity
                _TEAM_CONTEXT_RUNNING = True
                threading.Thread(
                    target=_run_team_context_tick,
                    name="team-context-main-tick",
                    daemon=True,
                ).start()

    if notice:
        try:
            from core.interface import get_central_contact_id

            contact_id = get_central_contact_id()
        except Exception:
            contact_id = ""
        if contact_id:
            with _TEAM_CONTEXT_LOCK:
                if _TEAM_CONTEXT_PENDING_NOTICE == notice:
                    _TEAM_CONTEXT_PENDING_NOTICE = ""
            return {contact_id: f"Team context synchronization notice:\n{notice}"}
    return None


EVENT_LOOP = [
    {
        "name": "check_team_context",
        "description": "Sync the Silicon team directory and advertising memory",
        "execute": check_team_context,
        "on_error": lambda e: print(f"[Team Context Error] {e}", flush=True),
    },
    {
        "name": "check_interface",
        "description": "Check for unread Silicon Interface events",
        "execute": get_unread_events,
        "on_error": lambda e: print(f"[Interface Error] {e}", flush=True),
    },
    {
        "name": "check_crons",
        "description": "Check if any cron jobs need to run",
        "execute": check_crons,
        "on_error": lambda e: print(f"[Cron Error] {e}", flush=True),
    },
    {
        "name": "check_manager_messages",
        "description": "Check for pending inter-manager messages",
        "execute": check_manager_messages,
        "on_error": lambda e: print(f"[Manager Messages Error] {e}", flush=True),
    },
    {
        "name": "check_system_updates",
        "description": "Check whether a Silicon system update is available",
        "execute": check_for_system_update,
        "on_error": lambda e: print(f"[Update Error] {e}", flush=True),
    },
    {
        "name": "check_workers",
        "description": "Check if any workers completed execution",
        "execute": check_completed_workers_formatted,
        "on_error": lambda e: print(f"[Worker Check Error] {e}", flush=True),
    },
    {
        "name": "clean_archives",
        "description": "Remove old worker archives",
        "execute": lambda: clean_old_archives(ARCHIVE_FOR),
        "on_error": lambda e: print(f"[Archive Cleanup Error] {e}", flush=True),
    },
]
