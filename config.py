import threading
import time

from core.interface import get_unread_events_durable
from core.cron import check_crons
from core.messages import check_manager_messages_durable
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
_TEAM_CONTEXT_RESULT_EPOCH = 0
_TEAM_CONTEXT_NEXT_SAFETY_CHECK = 0.0
_TEAM_CONTEXT_OWN_SIGNATURE = object()
TEAM_CONTEXT_MAIN_SAFETY_SECONDS = 5 * 60


def _team_context_result_detail(result):
    payload = result if isinstance(result, dict) else {}
    detail = payload.get("own_detail") or payload.get("detail") or ""
    return " ".join(str(detail).split())[:500]


def _team_context_own_is_healthy(result):
    payload = result if isinstance(result, dict) else {}
    own_status = str(payload.get("own_status") or "")
    healthy_statuses = {"current", "downloaded", "synced", "unchanged", "uploaded"}
    if own_status:
        return own_status in healthy_statuses
    return bool(payload.get("ok")) and str(payload.get("status") or "") in (
        healthy_statuses | {"updated"}
    )


def acknowledge_team_context_result(result):
    """Discard queued owner warnings superseded by a verified healthy result."""

    global _TEAM_CONTEXT_LAST_NOTICE
    global _TEAM_CONTEXT_PENDING_NOTICE
    global _TEAM_CONTEXT_RESULT_EPOCH
    if not _team_context_own_is_healthy(result):
        return False
    with _TEAM_CONTEXT_LOCK:
        _TEAM_CONTEXT_RESULT_EPOCH += 1
        _TEAM_CONTEXT_PENDING_NOTICE = ""
        _TEAM_CONTEXT_LAST_NOTICE = ""
    return True


def _team_context_notice(result):
    status = str((result or {}).get("own_status") or (result or {}).get("status") or "")
    if status == "conflict":
        return (
            "Your advertising-memory draft conflicts with a newer Glass revision. "
            "The local draft is preserved. Review it, then intentionally resolve "
            "with advertising_memory/update and resolve_conflict=true."
        )
    if status == "invalid":
        detail = _team_context_result_detail(result)
        if detail:
            return (
                "Your local advertising memory could not be published. "
                f"Sync detail: {detail} "
                "Content must be valid UTF-8, at most 100 lines and 65,536 bytes, "
                "with no NUL characters. If the content already passes those "
                "checks, verify that the runtime is reading a stable regular file "
                "from the expected Silicon data root."
            )
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

        with _TEAM_CONTEXT_LOCK:
            result_epoch = _TEAM_CONTEXT_RESULT_EPOCH
        activity = _TEAM_CONTEXT_MAINTENANCE_ACTIVITY
        if activity is not None:
            with heartbeat_scope([activity]):
                result = team_context_tick()
        else:
            result = team_context_tick()
        notice = _team_context_notice(result)
        with _TEAM_CONTEXT_LOCK:
            if result_epoch != _TEAM_CONTEXT_RESULT_EPOCH:
                # A successful explicit update superseded this in-flight tick.
                pass
            elif notice:
                if notice != _TEAM_CONTEXT_LAST_NOTICE:
                    _TEAM_CONTEXT_PENDING_NOTICE = notice
                    print(f"[Team Context] {notice}", flush=True)
                _TEAM_CONTEXT_LAST_NOTICE = notice
            elif _team_context_own_is_healthy(result):
                # A verified healthy owner state supersedes any warning that
                # was queued before a successful reconciliation or upload.
                _TEAM_CONTEXT_PENDING_NOTICE = ""
                _TEAM_CONTEXT_LAST_NOTICE = ""
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
    global _TEAM_CONTEXT_NEXT_SAFETY_CHECK
    global _TEAM_CONTEXT_OWN_SIGNATURE

    try:
        from core.team_context import own_advertising_signature

        own_signature = own_advertising_signature()
    except Exception:
        own_signature = None
    now = time.monotonic()

    with _TEAM_CONTEXT_LOCK:
        notice = _TEAM_CONTEXT_PENDING_NOTICE
        due = (
            now >= _TEAM_CONTEXT_NEXT_SAFETY_CHECK
            or own_signature != _TEAM_CONTEXT_OWN_SIGNATURE
        )
        if not _TEAM_CONTEXT_RUNNING and due:
            try:
                from core.maintenance import acquire_descendant_activity

                activity = acquire_descendant_activity(
                    "team_context_sync",
                    activity_id="periodic-team-context",
                )
            except Exception:
                activity = None
            if activity is not None:
                _TEAM_CONTEXT_OWN_SIGNATURE = own_signature
                _TEAM_CONTEXT_NEXT_SAFETY_CHECK = (
                    now + TEAM_CONTEXT_MAIN_SAFETY_SECONDS
                )
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
            deliver = False
            with _TEAM_CONTEXT_LOCK:
                if _TEAM_CONTEXT_PENDING_NOTICE == notice:
                    _TEAM_CONTEXT_PENDING_NOTICE = ""
                    deliver = True
            if deliver:
                return {
                    contact_id: f"Team context synchronization notice:\n{notice}"
                }
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
        "execute": get_unread_events_durable,
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
        "execute": check_manager_messages_durable,
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
