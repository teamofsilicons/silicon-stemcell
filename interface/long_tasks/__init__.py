"""A long task: the work a Carbon waits on, and the card they watch it through.

A long task outlives the manager turn that started it. It holds a lease so a
restart can tell a lifecycle that is still owned from one that was abandoned,
journals its workers before they start, queues its final reply durably, and
settles only once the card is terminal.

    constants  timings, caps, markers
    util       ids, fingerprints, goal estimates, liveness
    store      the document, its leases, and what may be recovered
    registry   the lifecycles this process owns
    queue      roots that arrived while a contact was busy
    lifecycle  the task itself, assembled from seven mixins
    runtime    starting one, and rebuilding what a restart interrupted
    reviews    accuracy-review roots
"""
import json  # noqa: F401  (tests reach for long_tasks.json)
import time  # noqa: F401  (tests patch long_tasks.time.time)

from interface.long_tasks.lifecycle import LongTaskLifecycle
from interface.long_tasks.constants import (
    ACCURACY_REVIEW_CLAIM_SECONDS,
    ACCURACY_REVIEW_CONTEXT_PREFIX,
    ACCURACY_REVIEW_SEGMENTS,
    ACTIVITY_HEARTBEAT_SECONDS,
    DURABLE_HEARTBEAT_SECONDS,
    LEASE_SECONDS,
    LONG_TASK_STATE_FILE,
    MAX_ACCURACY_REVIEW_CONTEXT_CHARS,
    MAX_ACTIVE_CONTACTS,
    MAX_ALIASES,
    MAX_PENDING_REPLY_ATTEMPTS,
    MAX_PENDING_REPLY_CHARS,
    MAX_PENDING_WORKERS,
    MAX_QUEUED_ROOTS,
    MAX_QUEUED_ROOTS_PER_CONTACT,
    MAX_RECOVERY_CONTACTS,
    MAX_STATE_CONTACTS,
    PREPARED_RECONCILE_GRACE_SECONDS,
    QUEUED_ROOT_LEASE_SECONDS,
    RETRY_MAX_SECONDS,
    STALE_ACTIVE_SECONDS,
    TOMBSTONE_SECONDS,
)
from interface.long_tasks.queue import (
    acknowledge_queued_long_task_root,
    claim_ready_long_task_roots,
    extract_accuracy_review_root,
    extract_queued_long_task_root_metadata,
    queue_long_task_root_if_blocked,
)
from interface.long_tasks.registry import (
    current_long_task,
    record_pending_worker_state,
    reset_long_task_registry_for_tests,
)
from interface.long_tasks.reviews import (
    accuracy_review_root_is_current,
    acknowledge_accuracy_review_dispatched,
    claim_ready_accuracy_review_roots,
    close_terminal_accuracy_lifecycle,
    complete_accuracy_review_root,
)
from interface.long_tasks.runtime import (
    backfill_active_estimated_task_lifecycles,
    begin_long_task_run,
    recover_long_task_lifecycles,
)

__all__ = [
    "ACCURACY_REVIEW_CLAIM_SECONDS",
    "ACCURACY_REVIEW_CONTEXT_PREFIX",
    "ACCURACY_REVIEW_SEGMENTS",
    "ACTIVITY_HEARTBEAT_SECONDS",
    "DURABLE_HEARTBEAT_SECONDS",
    "LEASE_SECONDS",
    "LONG_TASK_STATE_FILE",
    "LongTaskLifecycle",
    "MAX_ACCURACY_REVIEW_CONTEXT_CHARS",
    "MAX_ACTIVE_CONTACTS",
    "MAX_ALIASES",
    "MAX_PENDING_REPLY_ATTEMPTS",
    "MAX_PENDING_REPLY_CHARS",
    "MAX_PENDING_WORKERS",
    "MAX_QUEUED_ROOTS",
    "MAX_QUEUED_ROOTS_PER_CONTACT",
    "MAX_RECOVERY_CONTACTS",
    "MAX_STATE_CONTACTS",
    "PREPARED_RECONCILE_GRACE_SECONDS",
    "QUEUED_ROOT_LEASE_SECONDS",
    "RETRY_MAX_SECONDS",
    "STALE_ACTIVE_SECONDS",
    "TOMBSTONE_SECONDS",
    "accuracy_review_root_is_current",
    "acknowledge_accuracy_review_dispatched",
    "acknowledge_queued_long_task_root",
    "backfill_active_estimated_task_lifecycles",
    "begin_long_task_run",
    "claim_ready_accuracy_review_roots",
    "claim_ready_long_task_roots",
    "close_terminal_accuracy_lifecycle",
    "complete_accuracy_review_root",
    "current_long_task",
    "extract_accuracy_review_root",
    "extract_queued_long_task_root_metadata",
    "queue_long_task_root_if_blocked",
    "record_pending_worker_state",
    "recover_long_task_lifecycles",
    "reset_long_task_registry_for_tests",
]
