"""Keeping this Silicon's view of its team true, or admitting it is not.

Glass owns the team: who is in it, what each Silicon advertises, and which
revision is current. This package mirrors that locally and refuses to serve a
mirror it cannot verify.

Everything fails closed. If identity changed mid-flight, if a credential moved,
if a peer's bytes do not match their manifest hash, or if the team file does
not match the revision it claims — the mirror is blocked rather than read, and
a Carbon is told the team view is stale rather than shown a wrong one.

    constants   paths, limits, patterns
    errors      what can go wrong, body-free
    paths       where files live, and the only safe way to touch one
    visibility  the marker that says the mirror cannot be trusted
    manifest    one manifest entry, validated
    memory      advertising memory, bounded in both directions
    locks       the one lock that serializes all of this
    http        the only place this package talks to Glass
    state       what has been synced, and when
    context     the team document, validated and conditionally fetched
    drafts      keeping unsynced writing when identity changes underneath it
    peers       mirroring what other Silicons advertise
    identity    who we are to the team, and what changes when that changes
    own         our own memory, as the server holds it
    own_upload  the only path that writes it back
    own_sync    deciding what to do with it during a reconcile
    reconcile   one full pass, under the lock
    publish     the manager-facing write
    service     the two entrypoints the loop calls
    reads       verified reads, without the lock
"""
from interface.team.constants import (
    ADVERTISING_DIRECTORY,
    MAX_PARALLEL_PEER_SYNCS,
    MAX_TEAM_CONTEXT_BYTES,
    PROJECT_ROOT,
    TEAM_CONTEXT_PATH,
    TEAM_PLACEHOLDER_MARKDOWN,
)
from interface.team.errors import (
    TeamContextError,
    TeamContextIdentityChanged,
    TeamContextLockTimeout,
)
from interface.team.memory import (
    validate_advertised_memory,
    validate_advertising_memory,
)
from interface.team.paths import ensure_team_context_layout
from interface.team.publish import update_own_advertising_memory
from interface.team.reads import (
    own_advertising_signature,
    read_verified_team_advertising_memories,
    read_verified_team_markdown,
)
from interface.team.service import reconcile_team_context, team_context_tick

__all__ = [
    "ADVERTISING_DIRECTORY",
    "MAX_PARALLEL_PEER_SYNCS",
    "MAX_TEAM_CONTEXT_BYTES",
    "PROJECT_ROOT",
    "TEAM_CONTEXT_PATH",
    "TEAM_PLACEHOLDER_MARKDOWN",
    "TeamContextError",
    "TeamContextIdentityChanged",
    "TeamContextLockTimeout",
    "ensure_team_context_layout",
    "own_advertising_signature",
    "read_verified_team_advertising_memories",
    "read_verified_team_markdown",
    "reconcile_team_context",
    "team_context_tick",
    "update_own_advertising_memory",
    "validate_advertised_memory",
    "validate_advertising_memory",
]
