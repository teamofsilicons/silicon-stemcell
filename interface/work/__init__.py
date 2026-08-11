"""The durable work card a Carbon watches while their Silicon works.

A task, its todos, its milestones, its blockers, the workers under it, and the
calls between Silicons that belong to it. All of it lives in one document,
behind one lock, and is mirrored to Interface as it changes.

Call delivery is durable: an update that cannot be sent is journalled, leased,
and retried in lane order, so one call's updates arrive in the order they were
made and a failure holds its own lane rather than the whole queue.

    constants   tunables and vocabulary
    identity    ids, timestamps, result shapes
    store       the document and its lock
    payloads    what goes on the wire
    journal     what delivery is owed
    retry       one entry's lease and attempts
    cache       what a manager already knows
    correlation one call, mirrored across two managers
    activity    manager activity groups and progress frames
    delivery    getting an update there, and retrying until it lands
    updates     the work_update tool
    workers     what a worker is doing, on the card
    conversation, outbound_calls, inbound_calls, idle
"""
from interface.work.activity import (
    activity_frame_identity,
    begin_manager_activity,
    canonical_activity_state,
    current_manager_activity_group,
    settle_manager_activity,
)
from interface.work.cache import (
    active_task_id,
    refresh_task_snapshot,
)
from interface.work.constants import (
    ACTIVITY_STATE_ALIASES,
    CALL_IDLE_TIMEOUT_SECONDS,
    CALL_RETRY_ARCHIVE_LIMIT,
    CALL_RETRY_BASE_DELAY_SECONDS,
    CALL_RETRY_BATCH_LIMIT,
    CALL_RETRY_DEAD_LETTER_RETENTION_SECONDS,
    CALL_RETRY_DEDUPE_LIMIT,
    CALL_RETRY_DEDUPE_RETENTION_SECONDS,
    CALL_RETRY_LEASE_SECONDS,
    CALL_RETRY_MAX_ATTEMPTS,
    CALL_RETRY_MAX_DELAY_SECONDS,
    CALL_RETRY_MAX_ENTRIES,
    CALL_STATES,
    CANONICAL_ACTIVITY_STATES,
    MAX_CACHED_TASKS_PER_CONTACT,
    PENDING_CALL_TTL_SECONDS,
    TERMINAL_ACTIONS,
    TERMINAL_TASK_TTL_SECONDS,
    WORKER_STATES,
    WORK_UPDATES_FILE,
    WorkUpdateError,
)
from interface.work.conversation import (
    record_contact_call_message,
)
from interface.work.correlation import (
    touch_manager_call_activity,
)
from interface.work.delivery import (
    pending_call_update_retries,
    replay_pending_call_updates,
)
from interface.work.idle import (
    complete_inactive_calls,
    next_inactive_call_deadline,
)
from interface.work.inbound_calls import (
    enqueue_inbound_call,
    prepare_inbound_call,
    record_inbound_call,
)
from interface.work.outbound_calls import (
    enqueue_outbound_call,
    prepare_outbound_call,
    record_outbound_call,
)
from interface.work.updates import (
    WorkUpdates,
    execute_work_update,
    set_active_task_timer,
)
from interface.work.workers import (
    record_worker_started,
    record_worker_state,
)

__all__ = [
    "ACTIVITY_STATE_ALIASES",
    "CALL_IDLE_TIMEOUT_SECONDS",
    "CALL_RETRY_ARCHIVE_LIMIT",
    "CALL_RETRY_BASE_DELAY_SECONDS",
    "CALL_RETRY_BATCH_LIMIT",
    "CALL_RETRY_DEAD_LETTER_RETENTION_SECONDS",
    "CALL_RETRY_DEDUPE_LIMIT",
    "CALL_RETRY_DEDUPE_RETENTION_SECONDS",
    "CALL_RETRY_LEASE_SECONDS",
    "CALL_RETRY_MAX_ATTEMPTS",
    "CALL_RETRY_MAX_DELAY_SECONDS",
    "CALL_RETRY_MAX_ENTRIES",
    "CALL_STATES",
    "CANONICAL_ACTIVITY_STATES",
    "MAX_CACHED_TASKS_PER_CONTACT",
    "PENDING_CALL_TTL_SECONDS",
    "TERMINAL_ACTIONS",
    "TERMINAL_TASK_TTL_SECONDS",
    "WORKER_STATES",
    "WORK_UPDATES_FILE",
    "WorkUpdateError",
    "WorkUpdates",
    "active_task_id",
    "activity_frame_identity",
    "begin_manager_activity",
    "canonical_activity_state",
    "complete_inactive_calls",
    "current_manager_activity_group",
    "enqueue_inbound_call",
    "enqueue_outbound_call",
    "execute_work_update",
    "next_inactive_call_deadline",
    "pending_call_update_retries",
    "prepare_inbound_call",
    "prepare_outbound_call",
    "record_contact_call_message",
    "record_inbound_call",
    "record_outbound_call",
    "record_worker_started",
    "record_worker_state",
    "refresh_task_snapshot",
    "replay_pending_call_updates",
    "set_active_task_timer",
    "settle_manager_activity",
    "touch_manager_call_activity",
]
