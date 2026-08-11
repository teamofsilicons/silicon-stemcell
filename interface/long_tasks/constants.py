"""Timings, caps and markers for a long task.

Read as ``constants.X`` at call time: tests rebind the state file and patch the
reply-attempt cap as module attributes.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from interface import STATE_DIR


ACTIVITY_HEARTBEAT_SECONDS = max(
    1.0,
    float(os.environ.get("SILICON_ACTIVITY_HEARTBEAT_SECONDS", "12")),
)


DURABLE_HEARTBEAT_SECONDS = max(
    ACTIVITY_HEARTBEAT_SECONDS,
    float(os.environ.get("SILICON_DURABLE_HEARTBEAT_SECONDS", "90")),
)


RETRY_MAX_SECONDS = 60.0


LEASE_SECONDS = max(
    5.0,
    float(os.environ.get("SILICON_LONG_TASK_LEASE_SECONDS", "30")),
)


MAX_RECOVERY_CONTACTS = 64


MAX_ACTIVE_CONTACTS = 128


MAX_STATE_CONTACTS = 256


MAX_ALIASES = 64


MAX_PENDING_WORKERS = 64


MAX_PENDING_REPLY_CHARS = 262_144


MAX_PENDING_REPLY_ATTEMPTS = 12


PREPARED_RECONCILE_GRACE_SECONDS = 120.0


MAX_QUEUED_ROOTS = 128


MAX_QUEUED_ROOTS_PER_CONTACT = 16


QUEUED_ROOT_LEASE_SECONDS = 60.0


ACCURACY_REVIEW_SEGMENTS = 20


ACCURACY_REVIEW_CLAIM_SECONDS = 60.0


MAX_ACCURACY_REVIEW_CONTEXT_CHARS = 32_768


STALE_ACTIVE_SECONDS = 30 * 24 * 60 * 60


TOMBSTONE_SECONDS = 7 * 24 * 60 * 60


LONG_TASK_STATE_FILE = Path(STATE_DIR) / "long_task_updates.json"


_PROCESS_TOKEN = f"{os.getpid()}:{uuid.uuid4().hex}"


_SAFE_ACTIVITY_NOTES = {
    "reading_file": "Reviewing the relevant material",
    "writing_file": "Applying the current changes",
    "executing": "Running the current step",
    "searching_web": "Researching the current step",
    "thinking": "Working through the next step",
    "spawning_worker": "Workers are processing the request",
    "continuing": "Continuing with the next step",
    "working": "Work is still in progress",
}


_TERMINAL_ACTIONS = {"task/complete", "task/fail", "task/cancel"}


_TERMINAL_STATES = {"completed", "failed", "cancelled"}


_QUEUED_ROOT_MARKER = "durable_queued_root_id:"


_QUEUED_ROOT_VISIBILITY_MARKER = "durable_queued_root_visible:"


_ACCURACY_REVIEW_MARKER = "durable_accuracy_review_id:"


ACCURACY_REVIEW_CONTEXT_PREFIX = "Internal task accuracy review."
