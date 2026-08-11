"""What a contact currently has in flight.

Both the turn runner and the tracing around it need to know whether a contact
still has workers going — a turn that produced no reply is only a silent
failure if nothing is still running for that Carbon.
"""
from __future__ import annotations

from worker import list_active


def _contact_has_active_workers(carbon_id):
    try:
        return not list_active(carbon_id).startswith(
            "No active or queued workers."
        )
    except Exception:
        # Do not terminalize a task when worker state cannot be inspected
        # reliably.
        return True
