"""Tunables and vocabulary for the work-card protocol.

Every name is read as ``constants.X`` at call time: tests rebind the state file
and patch the retry limits as module attributes.
"""
from __future__ import annotations

from interface import (
    STATE_DIR,
)


WORK_UPDATES_FILE = STATE_DIR / "work_updates.json"


PENDING_CALL_TTL_SECONDS = 6 * 60 * 60


CALL_IDLE_TIMEOUT_SECONDS = 10.0


TERMINAL_TASK_TTL_SECONDS = 7 * 24 * 60 * 60


MAX_CACHED_TASKS_PER_CONTACT = 200


CALL_RETRY_BASE_DELAY_SECONDS = 1.0


CALL_RETRY_MAX_DELAY_SECONDS = 5 * 60.0


CALL_RETRY_BATCH_LIMIT = 20


CALL_RETRY_MAX_ATTEMPTS = 297


CALL_RETRY_LEASE_SECONDS = 90.0


CALL_RETRY_MAX_ENTRIES = 1_000


CALL_RETRY_DEAD_LETTER_RETENTION_SECONDS = 24 * 60 * 60


CALL_RETRY_ARCHIVE_LIMIT = 200


CALL_RETRY_DEDUPE_RETENTION_SECONDS = 7 * 24 * 60 * 60


CALL_RETRY_DEDUPE_LIMIT = 5_000


CANONICAL_ACTIVITY_STATES = {
    "thinking",
    "reading",
    "writing",
    "executing",
    "searching_web",
    "spawning_worker",
    "calling",
    "other",
    "done",
}


ACTIVITY_STATE_ALIASES = {
    "reading_file": "reading",
    "writing_file": "writing",
}


TERMINAL_ACTIONS = {
    "task/complete": ("complete", "completion", "completed"),
    "task/fail": ("fail", "failure", "failed"),
    "task/cancel": ("cancel", "cancellation", "cancelled"),
}


WORKER_STATES = {
    "yet_to_start",
    "in_progress",
    "completed",
    "blocked",
    "failed",
    "cancelled",
}


CALL_STATES = {"connecting", "in_progress", "completed", "failed", "cancelled"}


class WorkUpdateError(RuntimeError):
    """A manager-visible durable update failure."""
