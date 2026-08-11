"""Who this Silicon is to its team, and what changes when that changes.

An identity transition is the destructive case: the team file is invalidated,
visibility is blocked, peers are pruned, and drafts are archived, all before
anything new is believed.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import context as context_module
from interface.team import drafts as drafts_module
from interface.team import errors as errors_module
from interface.team import http as http_module
from interface.team import paths as paths_module
from interface.team import peers as peers_module
from interface.team import visibility as visibility_module
from pathlib import Path
from typing import Any
from interface.config import (
    authenticated_server_url,
)
from helpers.state import (
    fsync_directory,
)


def _fetch_identity(
    root: Path,
    *,
    config: dict[str, Any] | None = None,
    server_origin: str = "",
) -> dict[str, Any]:
    if config is None:
        config, configured_origin = http_module._load_config_snapshot(root)
        server_origin = server_origin or configured_origin
    else:
        server_origin = server_origin or authenticated_server_url(config)
    response = http_module._request(
        root,
        "GET",
        "/api/v1/silicons/me",
        config=config,
        timeout=8,
    )
    http_module._expect_status(response, {200}, "identity request")
    body = http_module._response_json(response, "identity request")
    if body.get("is_active") is False:
        raise errors_module.TeamContextError(
            "Glass reports that this Silicon is inactive.", status_code=403
        )
    silicon_id = paths_module._validate_identifier(body.get("silicon_id"), "Silicon ID")
    team_slug = paths_module._validate_identifier(
        body.get("owner_team_slug") or body.get("team"),
        "team slug",
    )
    return {
        "silicon_id": silicon_id,
        "team_slug": team_slug,
        "server_origin": http_module._validated_server_origin(server_origin),
        "credential_fingerprint": http_module._credential_fingerprint(config),
        "access_valid": True,
    }


def _stored_identity(state: dict[str, Any]) -> dict[str, Any] | None:
    identity = state.get("identity")
    if not isinstance(identity, dict):
        return None
    try:
        return {
            "silicon_id": paths_module._validate_identifier(
                identity.get("silicon_id"), "Silicon ID"
            ),
            "team_slug": paths_module._validate_identifier(identity.get("team_slug"), "team slug"),
            "server_origin": http_module._validated_server_origin(identity.get("server_origin")),
            "credential_fingerprint": http_module._validated_credential_fingerprint(
                identity.get("credential_fingerprint"),
                allow_empty=True,
            ),
            "access_valid": identity.get("access_valid") is not False,
        }
    except errors_module.TeamContextError:
        return None


def _cached_identity(state: dict[str, Any]) -> dict[str, Any] | None:
    identity = _stored_identity(state)
    if identity is None or not identity["access_valid"]:
        return None
    return identity


def _set_identity(state: dict[str, Any], identity: dict[str, Any]) -> bool:
    previous = _stored_identity(state)
    canonical = {
        "silicon_id": paths_module._validate_identifier(identity.get("silicon_id"), "Silicon ID"),
        "team_slug": paths_module._validate_identifier(identity.get("team_slug"), "team slug"),
        "server_origin": http_module._validated_server_origin(identity.get("server_origin")),
        "credential_fingerprint": http_module._validated_credential_fingerprint(
            identity.get("credential_fingerprint")
        ),
        "access_valid": True,
    }
    changed = previous != canonical
    if changed:
        principal_changed = previous is None or (
            previous["silicon_id"] != canonical["silicon_id"]
            or previous["server_origin"] != canonical["server_origin"]
        )
        state["identity"] = canonical
        state["context"] = {}
        if principal_changed:
            state["own"] = {}
    return changed


def _clear_visibility_block_if_verified(
    root: Path,
    state: dict[str, Any],
) -> None:
    identity = _cached_identity(state)
    context = state.get("context") or {}
    revision = str(context.get("revision") or "")
    if (
        identity is not None
        and context_module._context_matches_identity_scope(state, identity)
        and bool(constants._SHA256_RE.fullmatch(revision))
        and visibility_module._team_file_matches(root, revision)
    ):
        try:
            paths_module._visibility_block_file(root).unlink(missing_ok=True)
        except OSError:
            pass


def _invalidate_team_visibility(
    root: Path,
    state: dict[str, Any],
    *,
    preserve_ids: set[str] | None = None,
) -> int:
    """Fail closed while deleting only peer paths previously managed by Glass."""

    preserve_ids = preserve_ids or set()
    try:
        visibility_module._write_visibility_block(root)
    except OSError:
        # Context state and TEAM.md deletion below remain the primary boundary;
        # this marker closes the state-save/file-lock failure window.
        pass
    old_records = (
        dict(state.get("peers")) if isinstance(state.get("peers"), dict) else {}
    )
    old_ids = {
        item for item in state.get("managed_peer_ids", []) if isinstance(item, str)
    }
    removed, errors = peers_module._prune_stale_peers(
        root,
        old_ids,
        preserve_ids,
        old_records,
    )
    retained: dict[str, Any] = {}
    for silicon_id in errors:
        record = old_records.get(silicon_id)
        if isinstance(record, dict):
            retained[silicon_id] = record
    state["context"] = {}
    state["peers"] = retained
    state["managed_peer_ids"] = sorted(retained)
    try:
        paths_module._write_team_placeholder(root)
    except OSError:
        # Clearing the verified revision is sufficient for prompt reads to fail
        # closed even when the generated file is temporarily locked.
        pass
    return removed


def _mark_configured_scope_change(
    root: Path,
    state: dict[str, Any],
    server_origin: str,
    credential_fingerprint: str,
) -> bool:
    previous = _stored_identity(state)
    if previous is None or (
        previous["server_origin"] == server_origin
        and previous["credential_fingerprint"] == credential_fingerprint
    ):
        return False
    _invalidate_team_visibility(
        root,
        state,
        preserve_ids={previous["silicon_id"]},
    )
    state["identity"] = {**previous, "access_valid": False}
    return True


def _transition_identity(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
) -> tuple[bool, int]:
    previous = _stored_identity(state)
    canonical = {
        "silicon_id": paths_module._validate_identifier(identity.get("silicon_id"), "Silicon ID"),
        "team_slug": paths_module._validate_identifier(identity.get("team_slug"), "team slug"),
        "server_origin": http_module._validated_server_origin(identity.get("server_origin")),
        "credential_fingerprint": http_module._validated_credential_fingerprint(
            identity.get("credential_fingerprint")
        ),
        "access_valid": True,
    }
    if previous == canonical:
        return False, 0

    preserve_ids = {canonical["silicon_id"]}
    if previous is not None:
        preserve_ids.add(previous["silicon_id"])
    removed = _invalidate_team_visibility(
        root,
        state,
        preserve_ids=preserve_ids,
    )
    if previous is not None:
        # A verified transition must never leave the former identity available
        # as an offline-draft fallback if the remaining local work fails.
        state["identity"] = {**previous, "access_valid": False}
        if (
            previous["silicon_id"] != canonical["silicon_id"]
            or previous["server_origin"] != canonical["server_origin"]
        ):
            # If this write fails, do not accept the new identity in state and
            # do not proceed far enough to overwrite the former owner's path.
            drafts_module._archive_unsynced_own_draft(root, state, previous)
            if previous["silicon_id"] != canonical["silicon_id"]:
                # The old public path is no longer this process's own memory.
                # It may be recreated only from the new team's authenticated
                # peer manifest; unpublished work lives solely in the private
                # archive created above.
                old_path = paths_module._advertising_file(root, previous["silicon_id"])
                paths_module._assert_local_path(root, old_path)
                old_path.unlink(missing_ok=True)
                fsync_directory(old_path.parent)

    principal_changed = previous is not None and (
        previous["silicon_id"] != canonical["silicon_id"]
        or previous["server_origin"] != canonical["server_origin"]
    )
    if previous is None:
        drafts_module._quarantine_unscoped_advertising_files(
            root,
            state,
            preserve_ids={canonical["silicon_id"]},
        )
    changed = _set_identity(state, canonical)
    if principal_changed or previous is None:
        drafts_module._protect_new_own_scope(root, state, canonical)
    return changed, removed


def _invalidate_team_access(root: Path, state: dict[str, Any]) -> None:
    previous = _stored_identity(state)
    preserve_ids = {previous["silicon_id"]} if previous is not None else set()
    _invalidate_team_visibility(root, state, preserve_ids=preserve_ids)
    if previous is None:
        state["identity"] = {}
    else:
        state["identity"] = {**previous, "access_valid": False}
