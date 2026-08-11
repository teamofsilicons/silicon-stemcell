"""The two entrypoints the event loop and the operator call.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import context as context_module
from interface.team import errors as errors_module
from interface.team import http as http_module
from interface.team import identity as identity_module
from interface.team import locks as locks_module
from interface.team import own_sync as own_sync_module
from interface.team import paths as paths_module
from interface.team import reconcile as reconcile_module
from interface.team import state as state_module
from interface.team import visibility as visibility_module
import logging
import os
import time
from pathlib import Path
from typing import Any
from helpers.state import (
    fsync_directory,
)


log = logging.getLogger(__name__)


def _rollback_uncommitted_peer_files(
    root: Path,
    state: dict[str, Any],
    *,
    previous_identity: dict[str, Any] | None,
    previous_managed_ids: set[str],
) -> None:
    """Remove peer files whose deletion authority failed to reach disk."""

    current_identity = identity_module._stored_identity(state)
    current_managed_ids = state_module._managed_peer_ids(state)
    if previous_identity != current_identity:
        rollback_ids = current_managed_ids
    else:
        rollback_ids = current_managed_ids - previous_managed_ids
    current_own_id = (
        current_identity["silicon_id"] if current_identity is not None else ""
    )
    for silicon_id in sorted(rollback_ids - {current_own_id}):
        try:
            rollback_path = paths_module._advertising_file(root, silicon_id)
            paths_module._assert_local_path(root, rollback_path)
            rollback_path.unlink(missing_ok=True)
            fsync_directory(rollback_path.parent)
        except (OSError, errors_module.TeamContextError):
            pass


def reconcile_team_context(
    root: str | Path | None = None,
    *,
    force: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Reconcile TEAM.md and all advertising mirrors with Glass.

    ``force`` bypasses the team-context ETag, which is useful on startup and
    reconnect. Expected configuration, network, authentication, protocol, lock,
    and filesystem failures are swallowed and represented by the returned
    status. Temporary failures preserve last-known-good data. Confirmed access,
    team, Silicon, or Glass-origin changes fail closed immediately.
    """

    project_root = paths_module._normalise_root(root)
    try:
        try:
            paths_module.ensure_team_context_layout(project_root)
        except errors_module.TeamContextError as exc:
            return errors_module._safe_failure("invalid", detail=str(exc))
        with locks_module._sync_lock(project_root):
            state = state_module._load_state(project_root)
            previous_identity = identity_module._stored_identity(state)
            previous_managed_ids = state_module._managed_peer_ids(state)
            previous_had_context = bool(state.get("context"))
            try:
                previous_team_bytes = paths_module._read_regular_bytes(
                    project_root,
                    paths_module._team_file(project_root),
                    max_bytes=constants.MAX_TEAM_CONTEXT_BYTES,
                )
            except (OSError, ValueError):
                previous_team_bytes = None
            try:
                result = reconcile_module._reconcile_locked(
                    project_root,
                    state,
                    force=force,
                    reason=reason,
                )
            except Exception as exc:
                if errors_module._is_authoritative_access_failure(exc):
                    identity_module._invalidate_team_access(project_root, state)
                    result = errors_module._safe_failure("unauthorized")
                else:
                    result = errors_module._safe_failure("unavailable")
                state_module._record_reconcile_failure(state, now=time.time())
            try:
                state_module._save_state(project_root, state)
            except Exception:
                destructive_peer_change = bool(
                    result.get("peer_files_removed")
                    if isinstance(result, dict)
                    else False
                )
                if destructive_peer_change and not os.path.lexists(
                    paths_module._visibility_block_file(project_root)
                ):
                    try:
                        visibility_module._write_visibility_block(project_root)
                    except Exception:
                        pass
                _rollback_uncommitted_peer_files(
                    project_root,
                    state,
                    previous_identity=previous_identity,
                    previous_managed_ids=previous_managed_ids,
                )
                current_identity = identity_module._stored_identity(state)
                must_fail_closed = (
                    destructive_peer_change
                    or previous_identity != current_identity
                    or (previous_had_context and not state.get("context"))
                )
                try:
                    if previous_team_bytes is None or must_fail_closed:
                        paths_module._write_team_placeholder(project_root)
                    else:
                        paths_module._atomic_write_bytes(
                            project_root,
                            paths_module._team_file(project_root),
                            previous_team_bytes,
                        )
                except Exception:
                    pass
                return errors_module._safe_failure("state_error")
            if result.get("status") in {"current", "updated", "partial"}:
                identity_module._clear_visibility_block_if_verified(project_root, state)
            return result
    except errors_module.TeamContextLockTimeout:
        return errors_module._safe_failure("busy")
    except Exception:
        log.exception("team context reconciliation failed")
        return errors_module._safe_failure("unavailable")


def team_context_tick(root: str | Path | None = None) -> dict[str, Any]:
    """Cheap main-loop hook: hash the own file and run the 60-second fallback."""

    project_root = paths_module._normalise_root(root)
    try:
        try:
            paths_module.ensure_team_context_layout(project_root)
        except errors_module.TeamContextError as exc:
            return errors_module._safe_failure("invalid", detail=str(exc))
        with locks_module._sync_lock(project_root):
            state = state_module._load_state(project_root)
            previous_identity = identity_module._stored_identity(state)
            previous_managed_ids = state_module._managed_peer_ids(state)
            previous_had_context = bool(state.get("context"))
            try:
                previous_team_bytes = paths_module._read_regular_bytes(
                    project_root,
                    paths_module._team_file(project_root),
                    max_bytes=constants.MAX_TEAM_CONTEXT_BYTES,
                )
            except (OSError, ValueError):
                previous_team_bytes = None
            try:
                config, server_origin = http_module._load_config_snapshot(project_root)
                credential_fingerprint = http_module._credential_fingerprint(config)
                schedule = state.get("schedule") or {}
                next_reconcile_at = state_module._safe_schedule_timestamp(
                    schedule.get("next_reconcile_at"),
                    now=time.time(),
                    allow_future=True,
                )
                identity = identity_module._cached_identity(state)
                stored_identity = identity_module._stored_identity(state)
                scope_changed = (
                    stored_identity is not None
                    and stored_identity["access_valid"]
                    and (
                        stored_identity["server_origin"] != server_origin
                        or stored_identity["credential_fingerprint"]
                        != credential_fingerprint
                    )
                )
                needs_full_reconcile = (
                    identity is None
                    or scope_changed
                    or not state.get("context")
                    or (
                        identity is not None
                        and not context_module._context_matches_identity_scope(state, identity)
                    )
                    or os.path.lexists(paths_module._visibility_block_file(project_root))
                )
                due = scope_changed or time.time() >= next_reconcile_at
                if due:
                    result = reconcile_module._reconcile_locked(
                        project_root,
                        state,
                        force=False,
                        reason="fallback",
                        config=config,
                        server_origin=server_origin,
                    )
                elif needs_full_reconcile:
                    result = errors_module._safe_failure("deferred")
                else:
                    manifest = context_module._manifest_from_state(state)
                    if identity is None:
                        raise errors_module.TeamContextError("No cached Silicon identity.")
                    result = own_sync_module._sync_own(
                        project_root,
                        state,
                        identity,
                        manifest.get(identity["silicon_id"]),
                        config=config,
                    )
            except Exception as exc:
                if errors_module._is_authoritative_access_failure(exc):
                    identity_module._invalidate_team_access(project_root, state)
                    result = errors_module._safe_failure("unauthorized")
                else:
                    result = errors_module._safe_failure("unavailable")
                state_module._record_reconcile_failure(state, now=time.time())
            try:
                state_module._save_state(project_root, state)
            except Exception:
                destructive_peer_change = bool(
                    result.get("peer_files_removed")
                    if isinstance(result, dict)
                    else False
                )
                if destructive_peer_change and not os.path.lexists(
                    paths_module._visibility_block_file(project_root)
                ):
                    try:
                        visibility_module._write_visibility_block(project_root)
                    except Exception:
                        pass
                _rollback_uncommitted_peer_files(
                    project_root,
                    state,
                    previous_identity=previous_identity,
                    previous_managed_ids=previous_managed_ids,
                )
                current_identity = identity_module._stored_identity(state)
                must_fail_closed = (
                    destructive_peer_change
                    or previous_identity != current_identity
                    or (previous_had_context and not state.get("context"))
                )
                try:
                    if previous_team_bytes is None or must_fail_closed:
                        paths_module._write_team_placeholder(project_root)
                    else:
                        paths_module._atomic_write_bytes(
                            project_root,
                            paths_module._team_file(project_root),
                            previous_team_bytes,
                        )
                except Exception:
                    pass
                return errors_module._safe_failure("state_error")
            if result.get("status") in {"current", "updated", "partial"}:
                identity_module._clear_visibility_block_if_verified(project_root, state)
            return result
    except errors_module.TeamContextLockTimeout:
        return errors_module._safe_failure("busy")
    except Exception:
        log.exception("team context tick failed")
        return errors_module._safe_failure("unavailable")
