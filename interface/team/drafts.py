"""Keeping a Silicon's unsynced writing when its identity changes underneath it.

An identity transition is destructive: the old scope's files no longer belong
to this Silicon. Anything it wrote and had not yet published is archived
privately first, so a rename never costs a Carbon their words.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import memory as memory_module
from interface.team import paths as paths_module
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any
from helpers.state import (
    fsync_directory,
)


def _archive_unsynced_own_draft(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
) -> bool:
    """Privately preserve an unpublished draft before its path changes role."""

    path = paths_module._advertising_file(root, identity["silicon_id"])
    try:
        content, local_digest = memory_module._read_local_memory(root, path)
    except FileNotFoundError:
        return False
    except ValueError:
        # A regular file that exceeds the publication contract (including
        # invalid UTF-8) is still the former Silicon's work. Move it into the
        # private runtime archive before the same public path can become a peer
        # mirror. Symlinks and other special files are never followed.
        paths_module._assert_local_path(root, path)
        try:
            file_stat = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(file_stat.st_mode):
            return False

        archive_directory = (
            root / constants.DRAFT_ARCHIVE_DIRECTORY / identity["silicon_id"]
        )
        paths_module._ensure_private_archive_directory(root, archive_directory)
        fd, archive_name = tempfile.mkstemp(
            prefix="invalid-",
            suffix=".md",
            dir=str(archive_directory),
        )
        os.close(fd)
        archive_path = Path(archive_name)
        moved = False
        try:
            paths_module._assert_local_path(root, path)
            paths_module._ensure_private_archive_directory(root, archive_directory)
            paths_module._assert_local_path(root, archive_path)
            os.replace(path, archive_path)
            moved = True
            try:
                archive_path.chmod(0o600)
            except OSError:
                pass
            if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(
                    archive_directory,
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            fsync_directory(path.parent)
        except Exception:
            if not moved:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        archive_relative = archive_path.relative_to(root)
        metadata = {
            "silicon_id": identity["silicon_id"],
            "server_origin": identity["server_origin"],
            "path": archive_relative.as_posix(),
            "validation_status": "invalid",
            "byte_count": file_stat.st_size,
            "archived_at": time.time(),
        }
        _record_draft_archive(state, metadata)
        return True

    own = state.get("own") if isinstance(state.get("own"), dict) else {}
    base_digest = str(own.get("base_sha256") or "")
    has_base = own.get("silicon_id") == identity["silicon_id"] and bool(
        constants._SHA256_RE.fullmatch(base_digest)
    )
    if has_base and local_digest == base_digest:
        return False

    archive_relative = (
        Path(constants.DRAFT_ARCHIVE_DIRECTORY) / identity["silicon_id"] / f"{local_digest}.md"
    )
    archive_path = root / archive_relative
    paths_module._ensure_private_archive_directory(root, archive_path.parent)
    paths_module._atomic_write_bytes(root, archive_path, content.encode("utf-8"))

    metadata = {
        "silicon_id": identity["silicon_id"],
        "server_origin": identity["server_origin"],
        "path": archive_relative.as_posix(),
        "sha256": local_digest,
        "archived_at": time.time(),
    }
    _record_draft_archive(state, metadata)
    return True


def _record_draft_archive(
    state: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    archives = state.setdefault("draft_archives", [])
    if not isinstance(archives, list):
        archives = []
        state["draft_archives"] = archives
    archives[:] = [
        item
        for item in archives
        if not (isinstance(item, dict) and item.get("path") == metadata["path"])
    ]
    archives.append(metadata)


def _protect_new_own_scope(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
) -> None:
    """Require an explicit choice before publishing a pre-existing local file.

    The fixed advertising path can survive a Glass-origin or Silicon-ID change.
    Its contents belong to the old authority (or may be an old peer mirror), so
    an empty memory on the new authority must not turn a fallback reconcile into
    an implicit cross-authority publication.
    """

    path = paths_module._advertising_file(root, identity["silicon_id"])
    try:
        _content, local_digest = memory_module._read_local_memory(root, path)
    except FileNotFoundError:
        return
    except ValueError:
        state["own"] = {
            "silicon_id": identity["silicon_id"],
            "status": "invalid",
            "pending_sha256": "",
            "scope_changed": True,
        }
        return

    state["own"] = {
        "silicon_id": identity["silicon_id"],
        "status": "conflict",
        "pending_sha256": local_digest,
        "scope_changed": True,
    }


def _archive_explicit_draft(
    root: Path,
    state: dict[str, Any],
    content: str,
    *,
    identity: dict[str, Any] | None,
    server_origin: str = "",
) -> None:
    """Preserve an explicit edit privately when no principal can be verified."""

    raw = content.encode("utf-8")
    digest = paths_module._sha256(raw)
    silicon_id = identity["silicon_id"] if identity is not None else ""
    archive_scope = silicon_id or "unverified"
    archive_relative = Path(constants.DRAFT_ARCHIVE_DIRECTORY) / archive_scope / f"{digest}.md"
    archive_path = root / archive_relative
    paths_module._ensure_private_archive_directory(root, archive_path.parent)
    paths_module._atomic_write_bytes(root, archive_path, raw)
    _record_draft_archive(
        state,
        {
            "silicon_id": silicon_id,
            "server_origin": (
                identity["server_origin"] if identity is not None else server_origin
            ),
            "path": archive_relative.as_posix(),
            "sha256": digest,
            "validation_status": "valid",
            "reason": "identity_unverified",
            "archived_at": time.time(),
        },
    )


def _quarantine_unscoped_advertising_files(
    root: Path,
    state: dict[str, Any],
    *,
    preserve_ids: set[str],
) -> int:
    """Remove strict advertising paths whose Glass provenance was lost."""

    directory = root / constants.ADVERTISING_DIRECTORY
    paths_module._assert_local_path(root, directory)
    try:
        directory_stat = os.stat(directory, follow_symlinks=False)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise errors_module.TeamContextError(
                "Advertising-memory directory must be a local directory."
            )
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return 0

    archive_directory = root / constants.DRAFT_ARCHIVE_DIRECTORY / "unscoped"
    moved = 0
    for entry in entries:
        if entry.suffix != ".md":
            continue
        try:
            silicon_id = paths_module._validate_identifier(entry.stem, "Silicon ID")
        except errors_module.TeamContextError:
            continue
        if silicon_id in preserve_ids or entry.name != f"{silicon_id}.md":
            continue

        paths_module._assert_local_path(root, entry)
        entry_stat = os.stat(entry, follow_symlinks=False)
        if stat.S_ISLNK(entry_stat.st_mode):
            entry.unlink(missing_ok=True)
            fsync_directory(entry.parent)
            moved += 1
            continue

        paths_module._ensure_private_archive_directory(root, archive_directory)
        nonce = 0
        while True:
            archive_path = archive_directory / (
                f"{silicon_id}-{time.time_ns()}-{nonce}.quarantine"
            )
            if not os.path.lexists(archive_path):
                break
            nonce += 1
        paths_module._assert_local_path(root, entry)
        paths_module._ensure_private_archive_directory(root, archive_directory)
        paths_module._assert_local_path(root, archive_path)
        os.replace(entry, archive_path)
        fsync_directory(entry.parent)
        fsync_directory(archive_directory)
        if stat.S_ISREG(entry_stat.st_mode):
            try:
                archive_path.chmod(0o600)
            except OSError:
                pass
        archive_relative = archive_path.relative_to(root)
        _record_draft_archive(
            state,
            {
                "silicon_id": silicon_id,
                "server_origin": "",
                "path": archive_relative.as_posix(),
                "validation_status": "unscoped",
                "byte_count": (
                    entry_stat.st_size if stat.S_ISREG(entry_stat.st_mode) else None
                ),
                "reason": "state_provenance_missing",
                "archived_at": time.time(),
            },
        )
        moved += 1
    return moved
