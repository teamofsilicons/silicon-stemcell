"""Verified, policy-driven snapshots and Glass backup compatibility.

The durable local format is a content-addressed object store plus a canonical
manifest.  Files are copied and hashed in chunks, so snapshot and restore
memory use is bounded independently of data size.  The legacy Glass endpoint
still receives a disk-spooled ``backup.tar.gz`` multipart file and the public
``run_backup``/``build_archive`` APIs remain compatible.
"""
from __future__ import annotations
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import BinaryIO, Callable, Iterable, Iterator, Mapping, Sequence
try:  # Unix/macOS
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - Unix/macOS
    msvcrt = None
from core import data_policy
from core.glass import load_glass_config, silicon_api_request
from core.runtime_paths import DATA_ROOT
from core.state_store import (
    atomic_write_bytes,
    chmod_open_file,
    file_lock as state_file_lock,
    fsync_directory,
    lock_handle,
    unlock_handle,
)
MANIFEST_NAME = ".backupsilicon"
MANIFEST_ARCHIVE_PREFIX = ".backupsilicon.archive"
LEGACY_DEFAULT_MANIFEST = (
    "prompts/MEMORY.md",
    "prompts/memory/**",
    "prompts/LORE.md",
    "prompts/CONTACTS.md",
    "core/interface_state/contacts.json",
    "logs/**",
)
DEFAULT_MANIFEST: tuple[str, ...] = ()
MANIFEST_HEADER = (
    "# Optional additive backup paths. Mandatory protection is defined by "
    "core/data_policy.py."
)
UPLOAD_PATH = "/api/v1/silicon-backups/"
UPLOAD_TIMEOUT = 180
SNAPSHOT_SCHEMA = 1
CHUNK_SIZE = 1024 * 1024
DEFAULT_SPOOL_LIMIT = 8 * 1024 * 1024
DEFAULT_SNAPSHOT_RETENTION = 30
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_RESTORE_JOURNAL_BYTES = 1024 * 1024
MAX_UPDATE_JOURNAL_BYTES = 8 * 1024 * 1024
IN_PLACE_RESTORE_JOURNAL = Path(".silicon") / "restore-in-place.json"
IN_PLACE_RESTORE_LATEST = Path(".silicon") / "last-restored-snapshot.json"
RELEASE_SEQUENCE_FLOOR = ".silicon/release-sequence-floor.json"
RELEASE_SEQUENCE_FLOOR_LOCK = ".silicon/release-sequence-floor.lock"
MAINTENANCE_STATE = "core/interface_state/maintenance.json"
MAX_RELEASE_SEQUENCE_FLOOR_BYTES = 4096
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_RELEASE_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]{0,2})\."
    r"(0|[1-9][0-9]{0,2})\."
    r"(0|[1-9][0-9]{0,2})$"
)
_RELEASE_FLOOR_TRUST = {"git-semver-tag", "signed-ed25519"}
_MANIFEST_FILE_RE = re.compile(r"^([0-9a-f]{64})\.json$")
_OBJECT_PREFIX_RE = re.compile(r"^[0-9a-f]{2}$")
_OBJECT_SUFFIX_RE = re.compile(r"^[0-9a-f]{62}$")
_UPDATE_TRANSACTION_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)
_UPDATE_STATES = {
    "CREATED",
    "RESOLVED",
    "STAGED",
    "PLANNED",
    "DEPENDENCIES_READY",
    "DRAIN_REQUESTED",
    "QUIESCENT",
    "CHECKPOINTED",
    "STOPPING",
    "STOPPED",
    "ACTIVATED",
    "STARTED",
    "VALIDATED",
    "COMMITTED",
    "CANCELLED",
    "ROLLED_BACK",
    "FAILED",
}
_SNAPSHOT_LOCKS_GUARD = threading.Lock()
_SNAPSHOT_LOCKS: dict[str, threading.RLock] = {}
_SNAPSHOT_LOCK_LOCAL = threading.local()
class SnapshotError(RuntimeError):
    """A snapshot could not be safely created, checked, or restored."""
class SnapshotLimitError(SnapshotError):
    """The protected data exceeds configured safety limits."""
class SnapshotIntegrityError(SnapshotError):
    """A manifest, source file, or snapshot object failed verification."""
@dataclass(frozen=True)
class SnapshotLimits:
    """Explicit resource limits; streaming keeps memory below ``chunk_size``."""

    max_files: int = 250_000
    max_file_size: int = 8 * 1024 * 1024 * 1024
    max_total_size: int = 64 * 1024 * 1024 * 1024
    chunk_size: int = CHUNK_SIZE
    source_retries: int = 5

    def validate(self) -> None:
        if (
            self.max_files < 1
            or self.max_file_size < 0
            or self.max_total_size < 0
            or self.chunk_size < 4096
            or self.source_retries < 1
        ):
            raise ValueError("Snapshot limits must be positive and internally valid.")
@dataclass(frozen=True)
class SnapshotGCLimits:
    """Bounds filesystem work before GC is allowed to delete anything."""

    max_manifests: int = 100_000
    max_objects: int = 2_000_000
    max_unexpected_entries: int = 100_000

    def validate(self) -> None:
        if (
            self.max_manifests < 1
            or self.max_objects < 1
            or self.max_unexpected_entries < 0
        ):
            raise ValueError("Snapshot GC limits must be positive and valid.")
@dataclass(frozen=True)
class SnapshotResult:
    root_hash: str
    manifest_path: Path
    manifest: Mapping[str, object]
@dataclass(frozen=True)
class RestorePlan:
    root_hash: str
    release_id: str
    target: Path
    files: tuple[str, ...]
    tombstones: tuple[str, ...]
    total_size: int
    dry_run: bool
@dataclass(frozen=True)
class SnapshotGCPlan:
    """Deterministic retention decision for the canonical local store."""

    store: Path
    retain_latest: int
    protected_root_hashes: tuple[str, ...]
    retained_root_hashes: tuple[str, ...]
    delete_manifests: tuple[Path, ...]
    delete_objects: tuple[Path, ...]
    discarded_corrupt_manifests: tuple[Path, ...]
    unexpected_entries: tuple[Path, ...]
    reclaimable_bytes: int
    dry_run: bool
@dataclass(frozen=True)
class _ReleaseFloorPlan:
    """Minimum anti-rollback identity that an in-place restore must retain."""

    snapshot_entry: Mapping[str, object] | None
    snapshot_sequence: int | None
    snapshot_tree_sha256: str | None
    minimum_sequence: int
    minimum_tree_sha256: str
@dataclass(frozen=True)
class _ManifestRecord:
    root_hash: str
    path: Path
    mtime_ns: int
    size: int
