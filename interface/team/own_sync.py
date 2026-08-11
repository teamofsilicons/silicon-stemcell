"""Deciding what to do with own memory during a reconcile.

Download it, adopt it, upload ours, or record a conflict — and never guess:
an ambiguous state is recorded as a conflict for a Carbon to resolve.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import memory as memory_module
from interface.team import own as own_module
from interface.team import own_upload as own_upload_module
from interface.team import paths as paths_module
from pathlib import Path
from typing import Any


def _sync_own(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
    manifest_entry: dict[str, Any] | None,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = paths_module._advertising_file(root, identity["silicon_id"])
    own = state.get("own") if isinstance(state.get("own"), dict) else {}
    if own.get("silicon_id") != identity["silicon_id"]:
        own = {}
        state["own"] = own

    if not path.exists():
        remote, response_etag = own_module._fetch_own_remote(
            root,
            identity,
            config=config,
        )
        if manifest_entry and (
            remote["revision"] != manifest_entry["revision"]
            or remote["sha256"] != manifest_entry["sha256"]
        ):
            raise errors_module.TeamContextError("Glass own memory does not match the team manifest.")
        paths_module._atomic_write_bytes(root, path, remote["content"].encode("utf-8"))
        own_module._set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "downloaded",
            "changed": True,
            "local_saved": True,
            "revision": remote["revision"],
        }

    try:
        local_content, local_digest = memory_module._read_local_memory(root, path)
    except ValueError as exc:
        own["status"] = "invalid"
        own["pending_sha256"] = ""
        return {
            "ok": False,
            "status": "invalid",
            "changed": False,
            "local_saved": True,
            "detail": str(exc),
        }

    base_revision = own.get("base_revision")
    base_digest = str(own.get("base_sha256") or "")
    has_base = (
        isinstance(base_revision, int)
        and not isinstance(base_revision, bool)
        and base_revision >= 0
        and bool(constants._SHA256_RE.fullmatch(base_digest))
    )

    remote: dict[str, Any] | None = None
    response_etag = ""
    if manifest_entry is not None:
        remote_revision = manifest_entry["revision"]
        remote_digest = manifest_entry["sha256"]
    else:
        remote, response_etag = own_module._fetch_own_remote(
            root,
            identity,
            config=config,
        )
        remote_revision = remote["revision"]
        remote_digest = remote["sha256"]

    if own.get("status") == "conflict":
        conflict_revision = own.get("conflict_revision")
        remote_is_current_enough = (
            not isinstance(conflict_revision, int)
            or isinstance(conflict_revision, bool)
            or remote_revision >= conflict_revision
        )
        if local_digest == remote_digest and remote_is_current_enough:
            if remote is None:
                remote = {**manifest_entry, "content": local_content}
            own_module._set_own_base(state, identity, remote, etag=response_etag)
            return {
                "ok": True,
                "status": "unchanged",
                "changed": False,
                "local_saved": True,
                "revision": remote_revision,
            }
        own_module._mark_own_conflict(
            state,
            identity,
            pending_sha256=local_digest,
            actual_revision=remote_revision,
            remote_sha256=remote_digest,
        )
        return own_module._own_conflict_result(state["own"])

    if not has_base:
        if local_digest == remote_digest:
            if remote is None:
                remote = {**manifest_entry, "content": local_content}
            own_module._set_own_base(state, identity, remote, etag=response_etag)
            return {
                "ok": True,
                "status": "unchanged",
                "changed": False,
                "local_saved": True,
                "revision": remote_revision,
            }
        if remote_revision == 0 and remote_digest == constants._EMPTY_SHA256:
            return own_upload_module._upload_own(
                root,
                state,
                identity,
                local_content,
                remote_revision,
                config=config,
            )
        own["status"] = "conflict"
        own["pending_sha256"] = local_digest
        return {
            "ok": False,
            "status": "conflict",
            "changed": False,
            "local_saved": True,
            "actual_revision": remote_revision,
        }

    local_changed = local_digest != base_digest
    remote_content_changed = remote_digest != base_digest

    if local_digest == remote_digest:
        if remote is None:
            remote = {**manifest_entry, "content": local_content}
        own_module._set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "unchanged",
            "changed": False,
            "local_saved": True,
            "revision": remote_revision,
        }

    if not local_changed and remote_content_changed:
        if remote is None:
            remote, response_etag = own_module._fetch_own_remote(
                root,
                identity,
                config=config,
            )
            if (
                remote["revision"] != remote_revision
                or remote["sha256"] != remote_digest
            ):
                raise errors_module.TeamContextError(
                    "Glass own memory changed during reconciliation."
                )
        paths_module._atomic_write_bytes(root, path, remote["content"].encode("utf-8"))
        own_module._set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "downloaded",
            "changed": True,
            "local_saved": True,
            "revision": remote_revision,
        }

    if local_changed and not remote_content_changed:
        # A revision-only change with identical remote content is safe to adopt
        # before CAS; the local semantic change remains the only content change.
        expected_revision = remote_revision
        return own_upload_module._upload_own(
            root,
            state,
            identity,
            local_content,
            expected_revision,
            config=config,
        )

    if not local_changed:
        # Only the remote revision changed while the content remained identical.
        if remote is None:
            remote = {**manifest_entry, "content": local_content}
        own_module._set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "unchanged",
            "changed": False,
            "local_saved": True,
            "revision": remote_revision,
        }

    own["status"] = "conflict"
    own["pending_sha256"] = local_digest
    return {
        "ok": False,
        "status": "conflict",
        "changed": False,
        "local_saved": True,
        "actual_revision": remote_revision,
    }
