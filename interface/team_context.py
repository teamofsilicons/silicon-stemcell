"""The team package, under the name six call sites and thirteen tests use.

The implementation lives in ``interface/team/``. This stays because the module
path is a patch target in string form — ``interface.team_context.X`` — in tests
that no import-graph search would find.

ponytail: delete once those patch targets move to interface.team.<module>.
"""
from interface.team import (  # noqa: F401
    ADVERTISING_DIRECTORY,
    MAX_PARALLEL_PEER_SYNCS,
    MAX_TEAM_CONTEXT_BYTES,
    PROJECT_ROOT,
    TEAM_CONTEXT_PATH,
    TEAM_PLACEHOLDER_MARKDOWN,
    TeamContextError,
    TeamContextIdentityChanged,
    TeamContextLockTimeout,
    ensure_team_context_layout,
    own_advertising_signature,
    read_verified_team_advertising_memories,
    read_verified_team_markdown,
    reconcile_team_context,
    team_context_tick,
    update_own_advertising_memory,
    validate_advertised_memory,
    validate_advertising_memory,
)
