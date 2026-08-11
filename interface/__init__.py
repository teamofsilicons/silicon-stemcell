"""Everything Silicon says to the outside world.

Interface owns the wire: rooms, events, media, read receipts, STT/TTS,
take-back, remote browser, crons, backups, and canonical trust. Stemcell owns
its own contact cache, processed watermarks, and manager state.

This module is the front door. The pieces behind it:

    constants   paths, event vocabulary, timing
    errors      body-free failures that cross back into Silicon
    rpc         the CLI socket and its fallback
    client      one method per Interface command
    state       the local contact book and its lock
    contacts    fixed contact id to the room that reaches it
    events      reading one event's fields
    ingest      arrival: media, transcript, bookkeeping, manager context
    inbox       the durable inbox, its listener, and its commit boundary
    outbound    replies, progress, maintenance notices
    remote_browser  sharing a live browser, and taking a message back

Submodules import each other directly (``from interface.client import ...``),
never through this file, so importing any one of them stays cheap.
"""
from helpers.paths import STATE_DIR
from interface.client import InterfaceClient
from interface.constants import (
    CONTACTS_BACKUP_FILE,
    CONTACTS_FILE,
    DAEMON_DEEP_HEALTH_JITTER_SECONDS,
    DAEMON_DEEP_HEALTH_SECONDS,
    DAEMON_HEALTH_SECONDS,
    DEFAULT_INBOX_FILE,
    IGNORED_EVENT_TYPES,
    INBOX_CONSUMER_FILE,
    INBOX_POLL_SECONDS,
    INBOX_READ_CHUNK_BYTES,
    LEGACY_TELEGRAM_CONTACTS_FILE,
    MEDIA_DIR,
    PROJECT_ROOT,
    REMOTE_BROWSER_START_URL,
    REMOTE_BROWSER_STATE_FILE,
    RICH_MEDIA_RE,
    ROOM_SYNC_FALLBACK_SECONDS,
    RPC_MAX_RESPONSE_BYTES,
    RUNTIME_FILE_POLL_SECONDS,
    URL_RE,
    USER_VISIBLE_EVENT_TYPES,
    VALID_TRUST_LEVELS,
)
from interface.contacts import (
    discover_rooms,
    ensure_contact_for_target,
    get_own_profile,
    upsert_contact,
)
from interface.errors import (
    CallBookkeepingError,
    DurableHandoffError,
    InterfaceError,
    WorkCallMutationError,
)
from interface.inbox import (
    get_unread_events,
    get_unread_events_durable,
    maintenance_inbox_quiescent,
    notify_runtime_activity,
    runtime_file_notifications_active,
    start_listener,
    start_runtime_file_watch,
    stop_listener,
    stop_runtime_file_watch,
    wait_for_runtime_activity,
)
from interface.ingest import process_incoming_event
from interface.models import InboxRecord
from interface.outbound import (
    deliver_maintenance_notices,
    reply_contact,
    schedule_maintenance_notices,
    send_progress,
)
from interface.remote_browser import (
    complete_take_back,
    parse_remote_browser_url,
    remote_browser_close,
    remote_browser_share,
    take_back_event,
)
from interface.state import (
    apply_glass_trust_policy,
    get_central_contact_id,
    get_contact,
    get_contacts,
    validate_contacts_integrity,
)

__all__ = [
    "CONTACTS_BACKUP_FILE",
    "CONTACTS_FILE",
    "CallBookkeepingError",
    "DAEMON_DEEP_HEALTH_JITTER_SECONDS",
    "DAEMON_DEEP_HEALTH_SECONDS",
    "DAEMON_HEALTH_SECONDS",
    "DEFAULT_INBOX_FILE",
    "DurableHandoffError",
    "IGNORED_EVENT_TYPES",
    "INBOX_CONSUMER_FILE",
    "INBOX_POLL_SECONDS",
    "INBOX_READ_CHUNK_BYTES",
    "InboxRecord",
    "InterfaceClient",
    "InterfaceError",
    "LEGACY_TELEGRAM_CONTACTS_FILE",
    "MEDIA_DIR",
    "PROJECT_ROOT",
    "REMOTE_BROWSER_START_URL",
    "REMOTE_BROWSER_STATE_FILE",
    "RICH_MEDIA_RE",
    "ROOM_SYNC_FALLBACK_SECONDS",
    "RPC_MAX_RESPONSE_BYTES",
    "RUNTIME_FILE_POLL_SECONDS",
    "STATE_DIR",
    "URL_RE",
    "USER_VISIBLE_EVENT_TYPES",
    "VALID_TRUST_LEVELS",
    "WorkCallMutationError",
    "apply_glass_trust_policy",
    "complete_take_back",
    "deliver_maintenance_notices",
    "discover_rooms",
    "ensure_contact_for_target",
    "get_central_contact_id",
    "get_contact",
    "get_contacts",
    "get_own_profile",
    "get_unread_events",
    "get_unread_events_durable",
    "maintenance_inbox_quiescent",
    "notify_runtime_activity",
    "parse_remote_browser_url",
    "process_incoming_event",
    "remote_browser_close",
    "remote_browser_share",
    "reply_contact",
    "runtime_file_notifications_active",
    "schedule_maintenance_notices",
    "send_progress",
    "start_listener",
    "start_runtime_file_watch",
    "stop_listener",
    "stop_runtime_file_watch",
    "take_back_event",
    "upsert_contact",
    "validate_contacts_integrity",
    "wait_for_runtime_activity",
]
