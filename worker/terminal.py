"""The terminal worker.

Two differences from the base: it must already be registered before it can
start, and a launch that does not take drops its record rather than leaving a
registered worker that never ran.
"""
from __future__ import annotations

from worker.base import Worker
from worker.registry import _remove_worker_record


class TerminalWorker(Worker):
    worker_type = "terminal"
    requires_existing_record = True

    def _resume_arguments(self, record):
        # A stored empty provider means Claude, same as a missing one; the
        # launcher normalizes it either way.
        if not record:
            return "", ""
        from worker.process import _normalize_provider

        return (
            _normalize_provider(record.get("provider", "claude")),
            record.get("session_id", ""),
        )

    def _on_launch_failed(self, result: str) -> str:
        _remove_worker_record(self.worker_id)
        return result
