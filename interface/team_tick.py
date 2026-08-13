"""Keeping the team mirror fresh, on the event loop's schedule.

One reconcile at a time, coalesced: a second tick arriving while one is running
is dropped rather than queued, because reconciling twice against the same
revision achieves nothing. What a tick found is reported back to the manager
once — as a notice, not as a repeated warning.
"""

from __future__ import annotations

from interface import team

import threading
import time

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


ADVERTISING_FILE = "prompts/ADVERTISING.md"
ADVERTISING_PUBLISH_STATE = "interface/state/advertising_publish.json"


def _publish_own_advertising():
    """Carry ``prompts/ADVERTISING.md`` out to the team when it changes.

    Advertising memory is a plain file the Silicon edits directly — there is no
    command to publish it, by design, because a Silicon should describe itself
    by writing rather than by remembering to run something. So the sync tick
    watches the file and publishes it, and only when the content actually
    changed, so an unchanged file never burns a Glass revision.

    Returns the sync result when a publish was attempted, else ``None``.
    """
    import hashlib
    import os

    from helpers.paths import DATA_ROOT
    from helpers.state import read_json, write_json

    root = os.fspath(DATA_ROOT)
    path = os.path.join(root, ADVERTISING_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    state_path = os.path.join(root, ADVERTISING_PUBLISH_STATE)
    state = read_json(state_path, {"version": 1, "sha256": ""})
    if state.get("sha256") == digest:
        return None

    from interface.team import publish as team_publish

    result = team_publish.update_own_advertising_memory(content, root=root)
    if isinstance(result, dict) and result.get("ok"):
        write_json(state_path, {"version": 1, "sha256": digest})
    return result


def _run_team_context_tick():
    global _TEAM_CONTEXT_LAST_NOTICE
    global _TEAM_CONTEXT_PENDING_NOTICE
    global _TEAM_CONTEXT_RUNNING
    global _TEAM_CONTEXT_MAINTENANCE_ACTIVITY

    try:
        from silicon.runtime.maintenance import heartbeat_scope

        with _TEAM_CONTEXT_LOCK:
            result_epoch = _TEAM_CONTEXT_RESULT_EPOCH
        activity = _TEAM_CONTEXT_MAINTENANCE_ACTIVITY
        if activity is not None:
            with heartbeat_scope([activity]):
                published = _publish_own_advertising()
                result = team.team_context_tick()
        else:
            published = _publish_own_advertising()
            result = team.team_context_tick()
        # A failed publish is what the Silicon needs to hear about; a healthy
        # tick afterwards must not mask it.
        notice = _team_context_notice(published) or _team_context_notice(result)
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
                from silicon.runtime.maintenance import release_activity

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
        from interface.team import own_advertising_signature

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
                from silicon.runtime.maintenance import acquire_descendant_activity

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
            from interface import get_central_contact_id

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
