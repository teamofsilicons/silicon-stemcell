"""The marker that says the team file cannot be trusted right now.

Written fail-closed: if identity changed or the revision does not match, the
block goes down first and is only cleared once the file has been verified
against the identity that is current.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import paths as paths_module
import os
from pathlib import Path


def _write_visibility_block(root: Path) -> None:
    """Atomically hide TEAM.md before a destructive mirror transition."""

    path = paths_module._visibility_block_file(root)
    if os.path.lexists(path):
        return
    paths_module._atomic_write_bytes(root, path, b"")


def _team_file_matches(root: Path, revision: str) -> bool:
    try:
        return (
            paths_module._sha256(
                paths_module._read_regular_bytes(
                    root,
                    paths_module._team_file(root),
                    max_bytes=constants.MAX_TEAM_CONTEXT_BYTES,
                )
            )
            == revision
        )
    except (OSError, ValueError):
        return False
