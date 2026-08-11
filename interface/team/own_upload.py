"""The only path that writes this Silicon's memory to the server.

Identity is re-fetched and compared immediately before the PUT, against the
same credential snapshot the caller started with. If it moved, the upload is
refused rather than published under a scope that is no longer ours.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import http as http_module
from interface.team import identity as identity_module
from interface.team import memory as memory_module
from interface.team import own as own_module
from interface.team import paths as paths_module
from pathlib import Path
from typing import Any
from interface.config import (
    authenticated_server_url,
)


def _upload_own(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
    content: str,
    expected_revision: int,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Bind verification and mutation to one credential snapshot. If
    # the Interface config rotates between these two requests, the PUT still uses the
    # exact key whose identity was just verified.
    if config is None:
        config, server_origin = http_module._load_config_snapshot(root)
    else:
        server_origin = http_module._validated_server_origin(authenticated_server_url(config))
    live_identity = identity_module._fetch_identity(
        root,
        config=config,
        server_origin=server_origin,
    )
    if live_identity != identity:
        identity_module._transition_identity(root, state, live_identity)
        raise errors_module.TeamContextIdentityChanged(
            "Silicon identity changed before advertising-memory upload."
        )
    response = http_module._request(
        root,
        "PUT",
        "/api/v1/silicons/me/advertising-memory",
        body={
            "content": content,
            "expected_revision": expected_revision,
        },
        config=config,
        timeout=12,
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 409:
        try:
            body = http_module._response_json(response, "advertising-memory conflict")
        except errors_module.TeamContextError:
            body = {}
        actual_revision = body.get("actual_revision")
        if isinstance(actual_revision, bool) or not isinstance(actual_revision, int):
            actual_revision = None
        local_digest = paths_module._sha256(content.encode("utf-8"))
        remote_digest = ""
        try:
            remote, response_etag = own_module._fetch_own_remote(
                root,
                identity,
                config=config,
            )
            actual_revision = remote["revision"]
            remote_digest = remote["sha256"]
            if remote_digest == local_digest and remote["content"] == content:
                own_module._set_own_base(state, identity, remote, etag=response_etag)
                return {
                    "ok": True,
                    "status": "unchanged",
                    "changed": False,
                    "local_saved": True,
                    "revision": remote["revision"],
                }
        except Exception as exc:
            if errors_module._is_authoritative_access_failure(exc):
                raise
            # The 409 itself is authoritative enough to suppress blind retries;
            # a later conditional reconciliation will refresh missing metadata.
            pass
        own_module._mark_own_conflict(
            state,
            identity,
            pending_sha256=local_digest,
            actual_revision=actual_revision,
            remote_sha256=remote_digest,
        )
        return own_module._own_conflict_result(state["own"])
    http_module._expect_status(response, {200}, "own advertising-memory update")
    payload = http_module._response_json(response, "own advertising-memory update")
    remote = memory_module._validate_memory_payload(payload, identity["silicon_id"])
    local_digest = paths_module._sha256(content.encode("utf-8"))
    if remote["sha256"] != local_digest or remote["content"] != content:
        raise errors_module.TeamContextError(
            "Glass acknowledged different advertising-memory content."
        )
    own_module._set_own_base(state, identity, remote, etag=http_module._etag(response))
    return {
        "ok": True,
        "status": "uploaded" if payload.get("changed") is not False else "unchanged",
        "changed": payload.get("changed") is not False,
        "local_saved": True,
        "revision": remote["revision"],
    }


def _upload_explicit_own(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
    content: str,
    *,
    resolve_conflict: bool,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CAS an intentional manager edit without ever replacing its local draft."""

    own = state.get("own") if isinstance(state.get("own"), dict) else {}
    base_revision = own.get("base_revision")
    base_digest = str(own.get("base_sha256") or "")
    has_base = (
        own.get("silicon_id") == identity["silicon_id"]
        and isinstance(base_revision, int)
        and not isinstance(base_revision, bool)
        and base_revision >= 0
        and bool(constants._SHA256_RE.fullmatch(base_digest))
    )
    local_digest = paths_module._sha256(content.encode("utf-8"))

    if own.get("status") == "conflict" and not resolve_conflict:
        own_module._mark_own_conflict(
            state,
            identity,
            pending_sha256=local_digest,
            actual_revision=own.get("conflict_revision"),
            remote_sha256=str(own.get("conflict_sha256") or ""),
        )
        return own_module._own_conflict_result(state["own"])

    if resolve_conflict:
        remote, response_etag = own_module._fetch_own_remote(
            root,
            identity,
            config=config,
        )
        if remote["sha256"] == local_digest and remote["content"] == content:
            own_module._set_own_base(state, identity, remote, etag=response_etag)
            return {
                "ok": True,
                "status": "unchanged",
                "changed": False,
                "local_saved": True,
                "revision": remote["revision"],
            }
        return _upload_own(
            root,
            state,
            identity,
            content,
            remote["revision"],
            config=config,
        )

    if has_base:
        # Even an edit equal to the old base is sent. If another writer changed
        # Glass after that base, expected_revision produces a conflict rather
        # than allowing reconciliation to discard this explicit local choice.
        return _upload_own(
            root,
            state,
            identity,
            content,
            base_revision,
            config=config,
        )

    remote, response_etag = own_module._fetch_own_remote(
        root,
        identity,
        config=config,
    )
    if remote["sha256"] == local_digest:
        own_module._set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "unchanged",
            "changed": False,
            "local_saved": True,
            "revision": remote["revision"],
        }
    if remote["revision"] == 0 and remote["sha256"] == constants._EMPTY_SHA256:
        return _upload_own(
            root,
            state,
            identity,
            content,
            remote["revision"],
            config=config,
        )

    own_module._mark_own_conflict(
        state,
        identity,
        pending_sha256=local_digest,
        actual_revision=remote["revision"],
        remote_sha256=remote["sha256"],
    )
    return own_module._own_conflict_result(state["own"])
