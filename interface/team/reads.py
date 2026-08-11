"""Reading verified team content, without taking the lock.

Prompt assembly runs on every manager turn and must never block behind a
reconcile, so these read what has already been verified and nothing else.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import http as http_module
from interface.team import identity as identity_module
from interface.team import manifest as manifest_module
from interface.team import memory as memory_module
from interface.team import paths as paths_module
from interface.team import state as state_module
import os
import stat
from pathlib import Path
from typing import Any


def own_advertising_signature(
    root: str | Path | None = None,
) -> tuple[int, int, int, int] | None:
    """Return a content-change signature without performing reconciliation."""
    project_root = paths_module._normalise_root(root)
    try:
        state = state_module._load_state(project_root)
        identity = identity_module._stored_identity(state)
        if identity is None:
            return None
        metadata = os.stat(
            paths_module._advertising_file(project_root, identity["silicon_id"]),
            follow_symlinks=False,
        )
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )
    except (OSError, errors_module.TeamContextError):
        return None


def read_verified_team_markdown(
    root: str | Path | None = None,
    max_bytes: int = constants.MAX_TEAM_CONTEXT_BYTES,
) -> str:
    """Return only the last Glass-verified TEAM.md bytes, decoded as UTF-8.

    Prompt assembly can call this without taking the synchronization lock:
    generated files and state are replaced atomically, and a revision mismatch
    simply fails closed until the next read. Symlinks, oversized files, invalid
    UTF-8, missing state, and malformed revisions all return an empty string.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        return ""
    max_bytes = min(max_bytes, constants.MAX_TEAM_CONTEXT_BYTES)
    project_root = paths_module._normalise_root(root)
    path = paths_module._team_file(project_root)
    try:
        if os.path.lexists(paths_module._visibility_block_file(project_root)):
            return ""
        config, configured_origin = http_module._load_config_snapshot(project_root)
        configured_fingerprint = http_module._credential_fingerprint(config)
        state = state_module._load_state(project_root)
        identity = identity_module._cached_identity(state)
        context = state.get("context") or {}
        if (
            identity is None
            or configured_origin != identity["server_origin"]
            or configured_fingerprint != identity["credential_fingerprint"]
            or context.get("team_slug") != identity["team_slug"]
            or context.get("server_origin") != identity["server_origin"]
            or context.get("credential_fingerprint")
            != identity["credential_fingerprint"]
        ):
            return ""
        revision = str(context.get("revision") or "")
        if not constants._SHA256_RE.fullmatch(revision):
            return ""
        raw = paths_module._read_regular_bytes(project_root, path, max_bytes=max_bytes)
        if paths_module._sha256(raw) != revision:
            return ""
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError, errors_module.TeamContextError):
        return ""


def read_verified_team_advertising_memories(
    root: str | Path | None = None,
    *,
    expected_team_revision: str = "",
) -> list[dict[str, Any]]:
    """Return every locally mirrored advertising memory verified by Glass.

    The team manifest supplies the expected Silicon ID, path, revision, and
    digest. Prompt assembly receives a memory only when the local regular file
    still matches that manifest and the cached identity remains scoped to the
    configured Glass origin and credential. Missing, stale, malformed, or
    locally modified mirrors are omitted rather than exposed.
    """

    project_root = paths_module._normalise_root(root)
    try:
        if not read_verified_team_markdown(project_root):
            return []
        if os.path.lexists(paths_module._visibility_block_file(project_root)):
            return []
        config, configured_origin = http_module._load_config_snapshot(project_root)
        configured_fingerprint = http_module._credential_fingerprint(config)
        state = state_module._load_state(project_root)
        identity = identity_module._cached_identity(state)
        context = state.get("context") or {}
        if (
            identity is None
            or (
                expected_team_revision
                and context.get("revision") != expected_team_revision
            )
            or configured_origin != identity["server_origin"]
            or configured_fingerprint != identity["credential_fingerprint"]
            or context.get("team_slug") != identity["team_slug"]
            or context.get("server_origin") != identity["server_origin"]
            or context.get("credential_fingerprint")
            != identity["credential_fingerprint"]
        ):
            return []
        manifest = manifest_module._normalise_manifest(context.get("advertising_memories"))
        own_id = identity["silicon_id"]
        if own_id not in manifest:
            return []

        memories: list[dict[str, Any]] = []
        for silicon_id in sorted(manifest):
            entry = manifest[silicon_id]
            try:
                content, digest = memory_module._read_local_memory(
                    project_root,
                    paths_module._advertising_file(project_root, silicon_id),
                    allow_managed=silicon_id != own_id,
                )
            except (OSError, ValueError, errors_module.TeamContextError):
                continue
            if digest != entry["sha256"]:
                continue
            memories.append({**entry, "content": content})
        return memories
    except (OSError, ValueError, errors_module.TeamContextError):
        return []
