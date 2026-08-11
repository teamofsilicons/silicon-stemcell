"""One full reconcile, start to finish, under the lock.

Identity, then the context, then peers in parallel, then own memory, then the
prune. The peer fan-out cancels fail-closed: an authoritative access failure
from any peer stops the whole pass rather than publishing a partial mirror.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import context as context_module
from interface.team import errors as errors_module
from interface.team import http as http_module
from interface.team import identity as identity_module
from interface.team import memory as memory_module
from interface.team import own_sync as own_sync_module
from interface.team import paths as paths_module
from interface.team import peers as peers_module
from interface.team import state as state_module
from interface.team import visibility as visibility_module
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from interface.config import (
    authenticated_server_url,
)


def _reconcile_locked(
    root: Path,
    state: dict[str, Any],
    *,
    force: bool,
    reason: str,
    config: dict[str, Any] | None = None,
    server_origin: str = "",
) -> dict[str, Any]:
    del reason  # Kept out of persistent state and logs; callers receive status only.
    if config is None:
        config, configured_origin = http_module._load_config_snapshot(root)
        server_origin = server_origin or configured_origin
    else:
        server_origin = server_origin or authenticated_server_url(config)
    server_origin = http_module._validated_server_origin(server_origin)
    credential_fingerprint = http_module._credential_fingerprint(config)
    previous_peers = (
        dict(state.get("peers")) if isinstance(state.get("peers"), dict) else {}
    )
    if identity_module._stored_identity(state) is None and state.get("context"):
        # State written before origin scoping (or with corrupt identity fields)
        # cannot authorize prompt-visible data from an unknown Glass server.
        identity_module._invalidate_team_visibility(root, state)
    scope_changed = identity_module._mark_configured_scope_change(
        root,
        state,
        server_origin,
        credential_fingerprint,
    )
    identity = identity_module._fetch_identity(
        root,
        config=config,
        server_origin=server_origin,
    )
    identity_changed, transition_removed = identity_module._transition_identity(
        root,
        state,
        identity,
    )
    identity_changed = identity_changed or scope_changed

    response = context_module._get_context_response(
        root,
        identity,
        state,
        force=force or identity_changed,
        config=config,
    )
    status = http_module._expect_status(response, {200, 304}, "team-context request")
    context_changed = False

    if status == 304:
        context_state = state.get("context") or {}
        revision = str(context_state.get("revision") or "")
        try:
            manifest = context_module._manifest_from_state(state)
        except errors_module.TeamContextError:
            manifest = {}
        if (
            not manifest
            or not constants._SHA256_RE.fullmatch(revision)
            or not visibility_module._team_file_matches(root, revision)
            or not context_module._context_matches_identity_scope(state, identity)
        ):
            response = context_module._get_context_response(
                root,
                identity,
                state,
                force=True,
                config=config,
            )
            http_module._expect_status(response, {200}, "team-context repair request")
            status = 200

    if status == 200:
        payload = http_module._response_json(response, "team-context request")
        context, manifest = context_module._validate_context_payload(payload, identity)
        context_etag = http_module._etag(response)
        if not visibility_module._team_file_matches(root, context["revision"]):
            paths_module._atomic_write_bytes(
                root, paths_module._team_file(root), context["markdown"].encode("utf-8")
            )
            context_changed = True
        state["context"] = {
            **{key: value for key, value in context.items() if key != "markdown"},
            "etag": context_etag,
            "server_origin": server_origin,
            "credential_fingerprint": identity["credential_fingerprint"],
            "advertising_memories": context_module._manifest_for_state(manifest),
        }
    else:
        manifest = context_module._manifest_from_state(state)

    own_id = identity["silicon_id"]
    old_managed = {
        str(item) for item in state.get("managed_peer_ids", []) if isinstance(item, str)
    }
    current_peer_ids = set(manifest) - {own_id}
    old_peers = (
        {}
        if identity_changed
        else state.get("peers")
        if isinstance(state.get("peers"), dict)
        else {}
    )
    peer_ids = sorted(current_peer_ids)
    new_peers: dict[str, dict[str, Any]] = {}
    peer_changed = 0
    unverified_removed = 0
    errors: list[str] = []
    if peer_ids:
        # Grant deletion authority in memory before workers can publish peer
        # files. If any concurrent request proves that access was revoked, the
        # outer authoritative-failure handler can remove every file written by
        # this batch rather than leaking a newly downloaded, untracked peer.
        provisional_peers: dict[str, dict[str, Any]] = {}
        for silicon_id in peer_ids:
            old_record = old_peers.get(silicon_id)
            provisional_peers[silicon_id] = {
                **manifest[silicon_id],
                "etag": (
                    str(old_record.get("etag") or "")
                    if isinstance(old_record, dict)
                    else ""
                ),
            }
        state["peers"] = provisional_peers
        state["managed_peer_ids"] = peer_ids

        peer_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=min(constants.MAX_PARALLEL_PEER_SYNCS, len(peer_ids)),
            thread_name_prefix="team-peer-sync",
        )
        peer_futures: dict[
            Future[tuple[dict[str, Any], bool, bytes | None]],
            str,
        ] = {}
        peer_outcomes: dict[
            str,
            tuple[dict[str, Any], bool, bytes | None] | BaseException,
        ] = {}
        try:
            for silicon_id in peer_ids:
                old_record = old_peers.get(silicon_id)
                future = peer_executor.submit(
                    peers_module._sync_peer,
                    root,
                    identity,
                    manifest[silicon_id],
                    old_record if isinstance(old_record, dict) else {},
                    config=config,
                )
                peer_futures[future] = silicon_id

            # Detect a revoked credential in completion order so a slow request
            # for an alphabetically earlier peer cannot delay fail-closed
            # handling. Workers only stage validated bytes; they never publish
            # to the shared prompt tree.
            for future in as_completed(peer_futures):
                silicon_id = peer_futures[future]
                try:
                    peer_outcomes[silicon_id] = future.result()
                except Exception as exc:
                    if errors_module._is_authoritative_access_failure(exc):
                        for pending in peer_futures:
                            pending.cancel()
                        peer_executor.shutdown(
                            wait=False,
                            cancel_futures=True,
                        )
                        peer_executor = None
                        raise
                    peer_outcomes[silicon_id] = exc
        finally:
            if peer_executor is not None:
                peer_executor.shutdown(wait=True)

        # State and filesystem publication remain deterministic even though the
        # network fetches above completed out of order.
        for silicon_id in peer_ids:
            outcome = peer_outcomes[silicon_id]
            if isinstance(outcome, BaseException):
                errors.append(silicon_id)
                old_record = old_peers.get(silicon_id)
                if (
                    isinstance(old_record, dict)
                    and peers_module._peer_file_matches_record(
                        root,
                        silicon_id,
                        old_record,
                    )
                ):
                    new_peers[silicon_id] = old_record
                else:
                    try:
                        unverified_removed += int(
                            peers_module._remove_unverified_peer_file(root, silicon_id)
                        )
                    except (OSError, errors_module.TeamContextError) as exc:
                        identity_module._invalidate_team_visibility(
                            root,
                            state,
                            preserve_ids={own_id},
                        )
                        raise errors_module.TeamContextError(
                            "Could not hide an unverified peer advertising mirror."
                        ) from exc
                continue
            record, changed, staged_content = outcome
            try:
                if staged_content is not None:
                    paths_module._atomic_write_bytes(
                        root,
                        paths_module._advertising_file(root, silicon_id),
                        staged_content,
                    )
                new_peers[silicon_id] = record
                peer_changed += int(changed)
            except Exception:
                errors.append(silicon_id)
                old_record = old_peers.get(silicon_id)
                try:
                    _content, published_digest = memory_module._read_local_memory(
                        root,
                        paths_module._advertising_file(root, silicon_id),
                        allow_managed=True,
                    )
                except (OSError, ValueError):
                    published_digest = ""
                if published_digest == record["sha256"]:
                    # os.replace may have committed before a later directory
                    # fsync failed. Keep deletion authority for those verified
                    # bytes even though this pass remains partial.
                    new_peers[silicon_id] = record
                    peer_changed += int(changed)
                elif (
                    isinstance(old_record, dict)
                    and peers_module._peer_file_matches_record(
                        root,
                        silicon_id,
                        old_record,
                    )
                ):
                    new_peers[silicon_id] = old_record
                else:
                    try:
                        unverified_removed += int(
                            peers_module._remove_unverified_peer_file(root, silicon_id)
                        )
                    except (OSError, errors_module.TeamContextError) as exc:
                        identity_module._invalidate_team_visibility(
                            root,
                            state,
                            preserve_ids={own_id},
                        )
                        raise errors_module.TeamContextError(
                            "Could not hide an unverified peer advertising mirror."
                        ) from exc

    try:
        own_result = own_sync_module._sync_own(
            root,
            state,
            identity,
            manifest.get(own_id),
            config=config,
        )
    except errors_module.TeamContextIdentityChanged:
        raise
    except Exception as exc:
        if errors_module._is_authoritative_access_failure(exc):
            raise
        own_result = {
            "ok": False,
            "status": "unavailable",
            "changed": False,
            "local_saved": paths_module._advertising_file(root, own_id).exists(),
        }
        errors.append(own_id)

    removed, prune_errors = peers_module._prune_stale_peers(
        root,
        old_managed - {own_id},
        current_peer_ids,
        previous_peers,
    )
    removed += unverified_removed
    removed += transition_removed
    errors.extend(prune_errors)
    for silicon_id in prune_errors:
        old_record = previous_peers.get(silicon_id)
        if isinstance(old_record, dict):
            new_peers[silicon_id] = old_record
    state["peers"] = new_peers
    state["managed_peer_ids"] = sorted(current_peer_ids | set(prune_errors))
    own_ok = bool(own_result.get("ok", False))
    own_status = str(own_result.get("status") or "unavailable")
    own_detail = str(own_result.get("detail") or "").strip()
    has_issue = bool(errors) or not own_ok
    retry_soon = bool(errors) or (
        not own_ok and own_status not in {"conflict", "invalid"}
    )
    any_changed = bool(
        context_changed or peer_changed or removed or own_result.get("changed")
    )
    state_module._record_reconcile_success(
        state,
        now=time.time(),
        partial=retry_soon,
    )
    result = {
        "ok": not has_issue,
        "status": "partial" if has_issue else ("updated" if any_changed else "current"),
        "changed": any_changed,
        "context_changed": context_changed,
        "peer_files_changed": peer_changed,
        "peer_files_removed": removed,
        "own_status": own_status,
        "error_count": len(errors),
    }
    if own_detail:
        result["own_detail"] = own_detail
    return result
