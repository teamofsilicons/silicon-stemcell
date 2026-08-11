"""Manager activity groups, and the identity of one progress frame.
"""
from __future__ import annotations

from interface.work import constants
from interface.work import correlation as correlation_module
from interface.work import identity as identity_module
from interface.work import store as store_module
import uuid


def canonical_activity_state(state: str) -> str:
    normalized = constants.ACTIVITY_STATE_ALIASES.get(str(state or ""), str(state or ""))
    return normalized if normalized in constants.CANONICAL_ACTIVITY_STATES else "other"


def begin_manager_activity(contact_id: str, run_id: str = "") -> str:
    """Start or recover the stable manager-activity group for one inbound run."""
    seed = str(run_id or uuid.uuid4().hex)
    group_id = f"manager-run:{identity_module._safe_fragment(seed, uuid.uuid4().hex)}"
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        current = contact.get("activity")
        if not isinstance(current, dict) or current.get("run_id") != seed:
            contact["activity"] = {
                "run_id": seed,
                "group_id": group_id,
                "sequence": 0,
                "frames": {},
                "settled": False,
            }
            store_module._write_state(state)
        else:
            group_id = str(current.get("group_id") or group_id)
    correlation_module.touch_manager_call_activity(contact_id)
    return group_id


def current_manager_activity_group(contact_id: str) -> str:
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        activity = contact.get("activity")
        if not isinstance(activity, dict) or activity.get("settled"):
            return ""
        return str(activity.get("group_id") or "")


def activity_frame_identity(
    contact_id: str,
    group_id: str,
    *,
    frame_key: str = "",
    fingerprint: str = "",
) -> tuple[str, int, bool]:
    """Return (frame_id, revision, duplicate).

    A provider item keeps the same frame while its accepted representation
    changes.  Exact retries keep the same revision so Glass can replay them
    idempotently.
    """
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        activity = contact.setdefault("activity", {})
        if activity.get("group_id") != group_id:
            activity.clear()
            activity.update(
                {
                    "run_id": group_id,
                    "group_id": group_id,
                    "sequence": 0,
                    "frames": {},
                    "settled": False,
                }
            )
        frames = activity.setdefault("frames", {})
        if frame_key:
            key = identity_module._stable_id("frame-key", frame_key)
        else:
            activity["sequence"] = int(activity.get("sequence") or 0) + 1
            key = f"sequence:{activity['sequence']}"
        frame = frames.get(key)
        duplicate = False
        if not isinstance(frame, dict):
            frame = {
                "frame_id": identity_module._stable_id("activity", group_id, key),
                "revision": 0,
                "fingerprint": fingerprint,
            }
            frames[key] = frame
        elif fingerprint and frame.get("fingerprint") == fingerprint:
            duplicate = True
        else:
            frame["revision"] = int(frame.get("revision") or 0) + 1
            frame["fingerprint"] = fingerprint
        if len(frames) > 500:
            for stale_key in list(frames)[: len(frames) - 500]:
                if stale_key != key:
                    frames.pop(stale_key, None)
        store_module._write_state(state)
        return (
            str(frame["frame_id"]),
            int(frame.get("revision") or 0),
            duplicate,
        )


def settle_manager_activity(contact_id: str, group_id: str) -> None:
    with store_module._state_guard():
        state = store_module._read_state()
        contact = store_module._contact_state(state, contact_id)
        activity = contact.get("activity")
        if isinstance(activity, dict) and activity.get("group_id") == group_id:
            activity["settled"] = True
            store_module._write_state(state)
