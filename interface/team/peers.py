"""Mirroring what other Silicons in the team advertise.

A peer sync stages bytes and returns them; nothing is published from a worker
thread. Publication happens in one place, under the lock, in a determined
order — which is what makes the rollback of an unverified mirror possible.
"""
from __future__ import annotations

from interface.team import errors as errors_module
from interface.team import http as http_module
from interface.team import manifest as manifest_module
from interface.team import memory as memory_module
from interface.team import paths as paths_module
from interface.team import visibility as visibility_module
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote
from helpers.state import (
    fsync_directory,
)


def _fetch_peer(
    root: Path,
    identity: dict[str, Any],
    entry: dict[str, Any],
    *,
    etag: str = "",
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    headers = {"If-None-Match": etag} if etag else {}
    slug = quote(identity["team_slug"], safe="")
    silicon_id = quote(entry["silicon_id"], safe="")
    response = http_module._request(
        root,
        "GET",
        f"/api/v1/teams/{slug}/advertising-memories/{silicon_id}",
        headers=headers,
        config=config,
        timeout=10,
    )
    status = http_module._expect_status(response, {200, 304}, "peer advertising-memory request")
    if status == 304:
        return None, http_module._etag(response) or etag
    payload = http_module._response_json(response, "peer advertising-memory request")
    return (
        memory_module._validate_memory_payload(
            payload,
            entry["silicon_id"],
            expected=entry,
            allow_managed=True,
        ),
        http_module._etag(response),
    )


def _sync_peer(
    root: Path,
    identity: dict[str, Any],
    entry: dict[str, Any],
    old_record: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool, bytes | None]:
    """Fetch and validate one peer, leaving publication to the lock owner."""

    path = paths_module._advertising_file(root, entry["silicon_id"])
    try:
        _content, local_digest = memory_module._read_local_memory(
            root,
            path,
            allow_managed=True,
        )
    except (OSError, ValueError):
        local_digest = ""
    if local_digest == entry["sha256"]:
        return {
            **entry,
            "etag": str(old_record.get("etag") or ""),
        }, False, None

    remote, response_etag = _fetch_peer(
        root,
        identity,
        entry,
        etag=str(old_record.get("etag") or ""),
        config=config,
    )
    if remote is None:
        # A cached ETag cannot repair a missing or locally modified mirror.
        remote, response_etag = _fetch_peer(
            root,
            identity,
            entry,
            config=config,
        )
    if remote is None:
        raise errors_module.TeamContextError("Glass did not return the required peer memory.")
    return {
        **entry,
        "etag": response_etag,
    }, True, remote["content"].encode("utf-8")


def _peer_file_matches_record(
    root: Path,
    silicon_id: str,
    record: dict[str, Any],
) -> bool:
    """Return whether a peer mirror still matches a validated Glass record."""

    try:
        validated = manifest_module._manifest_entry(record)
        if validated["silicon_id"] != silicon_id:
            return False
        _content, local_digest = memory_module._read_local_memory(
            root,
            paths_module._advertising_file(root, silicon_id),
            allow_managed=True,
        )
        return local_digest == validated["sha256"]
    except (OSError, ValueError, errors_module.TeamContextError):
        return False


def _remove_unverified_peer_file(root: Path, silicon_id: str) -> bool:
    path = paths_module._advertising_file(root, silicon_id)
    paths_module._assert_local_path(root, path)
    existed = os.path.lexists(path)
    if existed:
        visibility_module._write_visibility_block(root)
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)
    return existed


def _prune_stale_peers(
    root: Path,
    old_ids: set[str],
    current_ids: set[str],
    old_records: dict[str, Any],
) -> tuple[int, list[str]]:
    removed = 0
    errors: list[str] = []
    for silicon_id in sorted(old_ids - current_ids):
        record = old_records.get(silicon_id)
        if not isinstance(record, dict):
            continue
        try:
            # Only a fully validated record written by an earlier successful
            # peer fetch grants deletion authority over the fixed local path.
            validated_record = manifest_module._manifest_entry(record)
            if validated_record["silicon_id"] != silicon_id:
                continue
        except errors_module.TeamContextError:
            continue
        try:
            path = paths_module._advertising_file(root, silicon_id)
            paths_module._assert_local_path(root, path)
            existed = os.path.lexists(path)
            if existed:
                visibility_module._write_visibility_block(root)
            path.unlink(missing_ok=True)
            fsync_directory(path.parent)
            removed += int(existed)
        except (OSError, errors_module.TeamContextError):
            errors.append(silicon_id)
    return removed, errors
