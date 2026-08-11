"""The one place worker/ talks to the maintenance coordinator.

A running worker holds a lease so an update cannot fence the instance out from
under it. Every call here is lazy-imported and swallows its own failures: a
coordinator that is unavailable must never stop a worker from starting, and it
must never stop a finished one from being reported.
"""
from __future__ import annotations


def _maintenance_reference():
    try:
        from manager.runtime.maintenance import current_activity

        activity = current_activity()
        return activity.reference() if activity is not None else {}
    except Exception:
        return {}


def _maintenance_activity(reference):
    try:
        from manager.runtime.maintenance import activity_from_reference

        return activity_from_reference(reference)
    except Exception:
        return None


def _heartbeat_maintenance_activity(reference):
    activity = _maintenance_activity(reference)
    if activity is None:
        return None
    try:
        from manager.runtime.maintenance import heartbeat_activity

        return activity if heartbeat_activity(activity) else None
    except Exception:
        return None


def _release_maintenance_activity(reference):
    activity = _maintenance_activity(reference)
    if activity is None:
        return False
    try:
        from manager.runtime.maintenance import release_activity

        return release_activity(activity)
    except Exception:
        return False


