"""Where every team file lives, and the only safe way to read or write one.

Each path is derived from a validated identifier and checked to be inside the
instance root before it is opened. Reads refuse to follow a symlink; writes go
through a temp file and a rename, so a half-written TEAM.md never exists.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
import hashlib
import os
import stat
from pathlib import Path
from typing import Any
from helpers.state import (
    atomic_write_bytes,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalise_root(root: str | Path | None) -> Path:
    return Path(root or constants.PROJECT_ROOT).resolve()


def _state_file(root: Path) -> Path:
    return root / constants.STATE_PATH


def _lock_file(root: Path) -> Path:
    return root / constants.LOCK_PATH


def _team_file(root: Path) -> Path:
    return root / constants.TEAM_CONTEXT_PATH


def _visibility_block_file(root: Path) -> Path:
    return root / constants.VISIBILITY_BLOCK_PATH


def _advertising_file(root: Path, silicon_id: str) -> Path:
    _validate_identifier(silicon_id, "Silicon ID")
    return root / constants.ADVERTISING_DIRECTORY / f"{silicon_id}.md"


def _validate_identifier(value: Any, label: str) -> str:
    value = str(value or "").strip()
    pattern = constants._TEAM_SLUG_RE if label == "team slug" else constants._SILICON_ID_RE
    if not pattern.fullmatch(value):
        raise errors_module.TeamContextError(f"Glass returned an invalid {label}.")
    return value


def _assert_local_path(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    parent_resolved = path.parent.resolve(strict=False)
    try:
        common = Path(os.path.commonpath((str(root_resolved), str(parent_resolved))))
    except ValueError as exc:
        raise errors_module.TeamContextError(
            "Generated context path escapes the Silicon root."
        ) from exc
    if common != root_resolved:
        raise errors_module.TeamContextError("Generated context path escapes the Silicon root.")


def _atomic_write_bytes(root: Path, path: Path, data: bytes) -> None:
    """Write inside the Silicon root only, then persist atomically."""
    _assert_local_path(root, path)
    atomic_write_bytes(path, data, dir_mode=None)


def _read_regular_bytes(
    root: Path,
    path: Path,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read one unchanged regular file without following a symbolic link."""

    _assert_local_path(root, path)
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Local context path must be a regular file.")
    if max_bytes is not None and before.st_size > max_bytes:
        raise ValueError("Local context file exceeds its size limit.")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise ValueError("Local context file changed while it was being opened.")
        if max_bytes is not None and opened.st_size > max_bytes:
            raise ValueError("Local context file exceeds its size limit.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("Local context file exceeds its size limit.")
            chunks.append(chunk)
        after = os.stat(path, follow_symlinks=False)
        if not os.path.samestat(opened, after):
            raise ValueError("Local context file changed while it was being read.")
        # Re-resolve the parent after reading so an ancestor symlink swap cannot
        # turn an external file into uploadable local advertising content.
        _assert_local_path(root, path)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _ensure_private_archive_directory(root: Path, path: Path) -> None:
    """Create one contained archive directory without accepting a symlink."""

    _assert_local_path(root, path)
    path.mkdir(parents=True, exist_ok=True)
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise errors_module.TeamContextError(
            "Advertising-memory draft archive must be a local directory."
        )
    # ``_assert_local_path`` validates a path's resolved parent.  Validate a
    # hypothetical child so this directory itself is included in containment
    # checking after it has been created.
    _assert_local_path(root, path / ".archive-containment-check")
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except (NotImplementedError, OSError):
        pass
    after = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(after.st_mode) or not os.path.samestat(before, after):
        raise errors_module.TeamContextError(
            "Advertising-memory draft archive changed during validation."
        )
    _assert_local_path(root, path / ".archive-containment-check")


def _write_team_placeholder(root: Path) -> None:
    _atomic_write_bytes(
        root,
        _team_file(root),
        constants.TEAM_PLACEHOLDER_MARKDOWN.encode("utf-8"),
    )


def ensure_team_context_layout(
    root: str | Path | None = None,
) -> dict[str, str]:
    """Ensure the pre-fetch TEAM placeholder and advertising directory exist."""

    project_root = _normalise_root(root)
    advertising_directory = project_root / constants.ADVERTISING_DIRECTORY
    _assert_local_path(project_root, advertising_directory)
    advertising_directory.mkdir(parents=True, exist_ok=True)
    directory_mode = os.lstat(advertising_directory).st_mode
    if stat.S_ISLNK(directory_mode) or not stat.S_ISDIR(directory_mode):
        raise errors_module.TeamContextError(
            "Advertising-memory path must be a local directory."
        )
    _assert_local_path(
        project_root,
        advertising_directory / ".layout-containment-check",
    )

    team_path = _team_file(project_root)
    if os.path.lexists(team_path):
        team_mode = os.lstat(team_path).st_mode
        if stat.S_ISLNK(team_mode) or not stat.S_ISREG(team_mode):
            raise errors_module.TeamContextError("TEAM.md must be a local regular file.")
    else:
        _write_team_placeholder(project_root)

    return {
        "team_path": constants.TEAM_CONTEXT_PATH,
        "advertising_directory": constants.ADVERTISING_DIRECTORY,
    }
