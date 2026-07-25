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
from contextlib import contextmanager
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

MANIFEST_NAME = ".backupsilicon"
MANIFEST_ARCHIVE_PREFIX = ".backupsilicon.archive"
# These paths used to be copied into every instance manifest.  They now live
# only in core.data_policy.  The tuple remains solely to migrate old manifests.
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
# Manual GC keeps its explicit bounded default for operator use. Automatic
# backup completion no longer invokes GC; every distinct local snapshot stays.
DEFAULT_SNAPSHOT_RETENTION = 30
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_RESTORE_JOURNAL_BYTES = 1024 * 1024
MAX_UPDATE_JOURNAL_BYTES = 8 * 1024 * 1024
IN_PLACE_RESTORE_JOURNAL = Path(".silicon") / "restore-in-place.json"
IN_PLACE_RESTORE_LATEST = Path(".silicon") / "last-restored-snapshot.json"
RELEASE_SEQUENCE_FLOOR = ".silicon/release-sequence-floor.json"
RELEASE_SEQUENCE_FLOOR_LOCK = ".silicon/release-sequence-floor.lock"
MAX_RELEASE_SEQUENCE_FLOOR_BYTES = 4096
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
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
    source_retries: int = 3

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


def _instance_root(start: str | os.PathLike | None = None) -> Path:
    if start:
        return Path(start).resolve()
    return DATA_ROOT


def _load_glass_config(root: Path) -> dict:
    config, _path = load_glass_config(root)
    return config


def _unique_manifest_archive_path(root: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = root / f"{MANIFEST_ARCHIVE_PREFIX}.{stamp}"
    candidate = base
    index = 1
    while candidate.exists() or candidate.is_symlink():
        index += 1
        candidate = root / f"{MANIFEST_ARCHIVE_PREFIX}.{stamp}.{index}"
    return candidate


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        _chmod_open_file(descriptor, temporary, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _chmod_open_file(descriptor: int, path: Path, mode: int) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, mode)
    else:  # pragma: no cover - exercised on Windows
        os.chmod(path, mode)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _snapshot_thread_lock(key: str) -> threading.RLock:
    with _SNAPSHOT_LOCKS_GUARD:
        return _SNAPSHOT_LOCKS.setdefault(key, threading.RLock())


def _lock_snapshot_handle(handle: BinaryIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_snapshot_handle(handle: BinaryIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _secure_lock_file(
    path: Path,
    *,
    label: str = "snapshot lock",
) -> BinaryIO:
    """Open the lock itself without permitting a symlink redirection."""

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not securely open {label}: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not os.path.samestat(opened, current)
        ):
            raise SnapshotIntegrityError(
                f"The {label} must be an unredirected regular file."
            )
        _chmod_open_file(descriptor, path, 0o600)
        return os.fdopen(descriptor, "r+b", buffering=0)
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def _snapshot_store_lock(root: Path) -> Iterator[None]:
    """Take the lock shared by snapshot writers and canonical-store GC."""

    root = Path(root).resolve(strict=True)
    state_root = root / ".silicon"
    if state_root.is_symlink():
        raise data_policy.UnsafePathError(
            "The in-instance .silicon state directory must not be a symbolic link."
        )
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        state_metadata = state_root.lstat()
    except OSError as exc:
        raise SnapshotIntegrityError(f"Could not inspect .silicon: {exc}") from exc
    if stat.S_ISLNK(state_metadata.st_mode) or not stat.S_ISDIR(
        state_metadata.st_mode
    ):
        raise SnapshotIntegrityError(".silicon must be a real directory.")

    lock_path = state_root / ".snapshots.lock"
    key = str(lock_path.absolute())
    thread_lock = _snapshot_thread_lock(key)
    with thread_lock:
        depths = getattr(_SNAPSHOT_LOCK_LOCAL, "depths", None)
        if depths is None:
            depths = {}
            _SNAPSHOT_LOCK_LOCAL.depths = depths
        depth = int(depths.get(key, 0))
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        handle = _secure_lock_file(lock_path)
        try:
            _lock_snapshot_handle(handle)
            depths[key] = 1
            yield
        finally:
            depths.pop(key, None)
            try:
                _unlock_snapshot_handle(handle)
            finally:
                handle.close()


@contextmanager
def _release_floor_lock(root: Path) -> Iterator[None]:
    """Serialize anti-rollback floor reads and replacements across processes."""

    root = Path(root).resolve(strict=True)
    state_root = root / ".silicon"
    if state_root.is_symlink():
        raise data_policy.UnsafePathError(
            "The in-instance .silicon state directory must not be a symbolic link."
        )
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        state_metadata = state_root.lstat()
    except OSError as exc:
        raise SnapshotIntegrityError(f"Could not inspect .silicon: {exc}") from exc
    if stat.S_ISLNK(state_metadata.st_mode) or not stat.S_ISDIR(
        state_metadata.st_mode
    ):
        raise SnapshotIntegrityError(".silicon must be a real directory.")

    lock_path = root / RELEASE_SEQUENCE_FLOOR_LOCK
    thread_lock = _snapshot_thread_lock(str(lock_path.absolute()))
    with thread_lock:
        handle = _secure_lock_file(
            lock_path,
            label="release sequence floor lock",
        )
        try:
            _lock_snapshot_handle(handle)
            try:
                opened = os.fstat(handle.fileno())
                current = lock_path.lstat()
            except OSError as exc:
                raise SnapshotIntegrityError(
                    f"Could not revalidate release sequence floor lock: {exc}"
                ) from exc
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or not os.path.samestat(opened, current)
            ):
                raise SnapshotIntegrityError(
                    "The release sequence floor lock changed while it was "
                    "being acquired."
                )
            yield
        finally:
            try:
                _unlock_snapshot_handle(handle)
            finally:
                handle.close()


def ensure_manifest_file(root: Path) -> list[str]:
    """Migrate a legacy directory and ensure the additive legacy manifest."""

    root = Path(root).resolve()
    path = root / MANIFEST_NAME
    archived: list[str] = []
    if path.is_symlink():
        raise data_policy.UnsafePathError(
            f"{MANIFEST_NAME} must not be a symbolic link."
        )
    if path.exists() and not path.is_file():
        archive = _unique_manifest_archive_path(root)
        path.rename(archive)
        archived.append(archive.name)
    if not path.is_file():
        _atomic_write(
            path,
            (MANIFEST_HEADER + "\n").encode("utf-8"),
            mode=0o600,
        )
    return archived


def read_manifest(root: Path) -> list[str]:
    """Read the legacy additive manifest with strict relative-path validation."""

    root = Path(root).resolve()
    ensure_manifest_file(root)
    path = root / MANIFEST_NAME
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    parsed: list[tuple[str, str | None]] = []
    present: set[str] = set()
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            parsed.append((line, None))
            continue
        pattern = data_policy.validate_relative_pattern(line)
        parsed.append((pattern, pattern))
        present.add(pattern)

    migrated = set(LEGACY_DEFAULT_MANIFEST).issubset(present)
    patterns: list[str] = []
    retained_lines: list[str] = []
    for line, pattern in parsed:
        if pattern is None:
            if line and line != MANIFEST_HEADER:
                retained_lines.append(line)
            continue
        if migrated and pattern in LEGACY_DEFAULT_MANIFEST:
            continue
        patterns.append(pattern)
        retained_lines.append(pattern)
    if migrated:
        updated = [MANIFEST_HEADER, *retained_lines]
        _atomic_write(
            path,
            ("\n".join(updated) + "\n").encode("utf-8"),
            mode=0o600,
        )
    return list(dict.fromkeys(patterns))


def load_policy(root: Path) -> data_policy.DataPolicy:
    """Return the canonical mandatory policy plus legacy/local additions."""

    root = Path(root).resolve(strict=True)
    return data_policy.load_data_policy(root, legacy_patterns=read_manifest(root))


def installed_release_id(root: Path) -> str:
    """Return the active immutable generation identity when one is available."""

    root = Path(root).resolve(strict=True)
    pointer = root / ".silicon" / "current.json"
    if not pointer.exists() and not pointer.is_symlink():
        return "legacy-unversioned"
    if pointer.is_symlink():
        raise data_policy.UnsafePathError(
            "The active release pointer must not be a symbolic link."
        )
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(f"Invalid active release pointer: {exc}") from exc
    if not isinstance(value, dict):
        raise SnapshotIntegrityError("The active release pointer must be an object.")
    for candidate in (
        value.get("generation_id"),
        (value.get("release") or {}).get("tree_sha256")
        if isinstance(value.get("release"), dict)
        else None,
        value.get("upstream_tree_sha256"),
    ):
        if candidate:
            return _normalise_release_id(str(candidate))
    return "legacy-unversioned"


def _safe_source_open(
    root: Path, relative_path: str
) -> tuple[BinaryIO, os.stat_result]:
    relative = data_policy.validate_relative_path(relative_path)
    root = root.resolve(strict=True)
    candidate = root.joinpath(*Path(relative).parts)
    cursor = root
    for component in Path(relative).parts:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"Protected source disappeared: {relative}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise data_policy.UnsafePathError(
                f"Refusing symbolic link in protected data: {relative}"
            )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not securely open protected source: {relative}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise data_policy.UnsafePathError(
                f"Refusing non-regular protected data: {relative}"
            )
        if not os.path.samestat(metadata, opened):
            raise SnapshotIntegrityError(
                f"Protected source changed while opening: {relative}"
            )
        return os.fdopen(descriptor, "rb"), opened
    except Exception:
        os.close(descriptor)
        raise


def _stat_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _object_path(store: Path, digest: str) -> Path:
    if not _DIGEST_RE.fullmatch(digest):
        raise SnapshotIntegrityError(f"Invalid content digest: {digest!r}")
    return store / "objects" / "sha256" / digest[:2] / digest[2:]


def _hash_file(path: Path, chunk_size: int) -> tuple[str, int]:
    if path.is_symlink():
        raise SnapshotIntegrityError(f"Snapshot object is a symbolic link: {path}")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise SnapshotIntegrityError(f"Missing snapshot object: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SnapshotIntegrityError(f"Snapshot object is not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SnapshotIntegrityError(f"Could not open snapshot object: {path}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not os.path.samestat(metadata, opened):
            raise SnapshotIntegrityError(
                f"Snapshot object changed while opening: {path}"
            )
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _verify_object(
    store: Path, digest: str, expected_size: int, chunk_size: int
) -> Path:
    path = _object_path(store, digest)
    actual_digest, actual_size = _hash_file(path, chunk_size)
    if actual_digest != digest or actual_size != expected_size:
        raise SnapshotIntegrityError(
            f"Snapshot object {digest} failed its SHA-256/size check."
        )
    return path


def _open_verified_object(
    store: Path,
    digest: str,
    expected_size: int,
    chunk_size: int,
) -> BinaryIO:
    path = _object_path(store, digest)
    if path.is_symlink():
        raise SnapshotIntegrityError(f"Snapshot object is a symbolic link: {path}")
    try:
        before = path.stat()
    except OSError as exc:
        raise SnapshotIntegrityError(f"Missing snapshot object: {path}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotIntegrityError(f"Snapshot object is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SnapshotIntegrityError(f"Could not open snapshot object: {path}") from exc
    try:
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or not os.path.samestat(before, opened)
            or opened.st_size != expected_size
        ):
            raise SnapshotIntegrityError(
                f"Snapshot object changed after verification: {digest}"
            )
        calculated = hashlib.sha256()
        size = 0
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            calculated.update(chunk)
        after = os.fstat(handle.fileno())
        if (
            calculated.hexdigest() != digest
            or size != expected_size
            or _stat_signature(opened) != _stat_signature(after)
        ):
            raise SnapshotIntegrityError(
                f"Snapshot object {digest} failed its SHA-256/size check."
            )
        handle.seek(0)
        return handle
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        elif "handle" in locals():
            handle.close()
        raise


def _prepare_store(root: Path, requested: Path | None) -> Path:
    """Create a local store without following an in-instance symlink."""

    if requested is not None:
        store = Path(requested).absolute()
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
        return store.resolve(strict=True)

    state_root = root / ".silicon"
    if state_root.is_symlink():
        raise data_policy.UnsafePathError(
            "The in-instance .silicon state directory must not be a symbolic link."
        )
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = state_root / "snapshots"
    if store.is_symlink():
        raise data_policy.UnsafePathError(
            "The in-instance snapshot store must not be a symbolic link."
        )
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    return store.resolve(strict=True)


def _copy_source_to_object(
    root: Path,
    protected: data_policy.ProtectedFile,
    store: Path,
    limits: SnapshotLimits,
) -> tuple[str, int, int]:
    """Copy a stable source version to the object store, retrying active writes."""

    temporary_directory = store / "objects" / ".tmp"
    temporary_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    last_reason = "changed while being copied"
    for _attempt in range(limits.source_retries):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="object.",
            dir=temporary_directory,
        )
        temporary = Path(temporary_name)
        try:
            _chmod_open_file(descriptor, temporary, 0o600)
            source, before = _safe_source_open(root, protected.relative_path)
            try:
                if before.st_size > limits.max_file_size:
                    raise SnapshotLimitError(
                        f"{protected.relative_path} exceeds max_file_size."
                    )
                digest = hashlib.sha256()
                copied = 0
                with os.fdopen(descriptor, "wb") as destination:
                    descriptor = -1
                    while True:
                        chunk = source.read(limits.chunk_size)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > limits.max_file_size:
                            raise SnapshotLimitError(
                                f"{protected.relative_path} exceeds max_file_size."
                            )
                        digest.update(chunk)
                        destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                after = os.fstat(source.fileno())
            finally:
                source.close()
            if copied != before.st_size or _stat_signature(before) != _stat_signature(
                after
            ):
                last_reason = "changed while being copied"
                continue
            hexdigest = digest.hexdigest()
            object_path = _object_path(store, hexdigest)
            object_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if object_path.exists() or object_path.is_symlink():
                _verify_object(store, hexdigest, copied, limits.chunk_size)
            else:
                os.replace(temporary, object_path)
                os.chmod(object_path, 0o400)
                _fsync_directory(object_path.parent)
            return hexdigest, copied, stat.S_IMODE(before.st_mode) & 0o777
        except SnapshotLimitError:
            raise
        except (OSError, SnapshotIntegrityError) as exc:
            last_reason = str(exc)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
    raise SnapshotIntegrityError(
        f"Could not take a stable snapshot of {protected.relative_path}: {last_reason}"
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalise_release_id(release_id: str) -> str:
    value = str(release_id).strip()
    if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
        raise SnapshotError("release_id must be a non-empty printable string.")
    return value


def _manifest_body(
    *,
    release_id: str,
    files: Sequence[Mapping[str, object]],
    tombstones: Iterable[str],
) -> dict[str, object]:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "release_id": _normalise_release_id(release_id),
        "files": list(files),
        "tombstones": sorted(
            {data_policy.validate_relative_path(path) for path in tombstones},
        ),
    }


def _validate_manifest(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise SnapshotIntegrityError("Snapshot manifest must be a JSON object.")
    if set(raw) != {"schema", "release_id", "files", "tombstones", "root_hash"}:
        raise SnapshotIntegrityError("Snapshot manifest has unknown or missing fields.")
    if raw.get("schema") != SNAPSHOT_SCHEMA:
        raise SnapshotIntegrityError(
            f"Unsupported snapshot schema: {raw.get('schema')!r}"
        )
    try:
        release_id = _normalise_release_id(str(raw.get("release_id", "")))
    except SnapshotError as exc:
        raise SnapshotIntegrityError(str(exc)) from exc
    files_raw = raw.get("files")
    tombstones_raw = raw.get("tombstones")
    if not isinstance(files_raw, list) or not isinstance(tombstones_raw, list):
        raise SnapshotIntegrityError("Snapshot files/tombstones must be arrays.")

    files: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in files_raw:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "sha256",
            "size",
            "mode",
            "classes",
        }:
            raise SnapshotIntegrityError("Snapshot file entry has invalid fields.")
        try:
            relative = data_policy.validate_relative_path(str(entry["path"]))
        except data_policy.DataPolicyError as exc:
            raise SnapshotIntegrityError(str(exc)) from exc
        if relative in seen or data_policy.is_known_secret(relative):
            raise SnapshotIntegrityError(
                f"Duplicate or forbidden snapshot path: {relative}"
            )
        seen.add(relative)
        digest = str(entry["sha256"])
        size = entry["size"]
        mode = entry["mode"]
        classes = entry["classes"]
        if not _DIGEST_RE.fullmatch(digest):
            raise SnapshotIntegrityError(f"Invalid SHA-256 for {relative}.")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise SnapshotIntegrityError(f"Invalid size for {relative}.")
        if (
            not isinstance(mode, int)
            or isinstance(mode, bool)
            or mode < 0
            or mode > 0o777
        ):
            raise SnapshotIntegrityError(f"Invalid mode for {relative}.")
        if (
            not isinstance(classes, list)
            or not classes
            or not all(isinstance(item, str) and item for item in classes)
            or classes != sorted(set(classes))
        ):
            raise SnapshotIntegrityError(f"Invalid classes for {relative}.")
        files.append(
            {
                "path": relative,
                "sha256": digest,
                "size": size,
                "mode": mode,
                "classes": classes,
            }
        )
    if [entry["path"] for entry in files] != sorted(seen):
        raise SnapshotIntegrityError("Snapshot files are not canonically sorted.")

    tombstones: list[str] = []
    for value in tombstones_raw:
        if not isinstance(value, str):
            raise SnapshotIntegrityError("Snapshot tombstones must be strings.")
        try:
            relative = data_policy.validate_relative_path(value)
        except data_policy.DataPolicyError as exc:
            raise SnapshotIntegrityError(str(exc)) from exc
        if relative in seen:
            raise SnapshotIntegrityError(
                f"Snapshot path is both live and tombstoned: {relative}"
            )
        tombstones.append(relative)
    if tombstones != sorted(set(tombstones)):
        raise SnapshotIntegrityError("Snapshot tombstones are not canonical.")

    body = _manifest_body(
        release_id=release_id,
        files=files,
        tombstones=tombstones,
    )
    root_hash = str(raw.get("root_hash", ""))
    calculated = hashlib.sha256(_canonical_json(body)).hexdigest()
    if not _DIGEST_RE.fullmatch(root_hash) or root_hash != calculated:
        raise SnapshotIntegrityError("Snapshot root hash does not match its manifest.")
    return {**body, "root_hash": root_hash}


def _manifest_path(store: Path, root_hash: str) -> Path:
    if not _DIGEST_RE.fullmatch(root_hash):
        raise SnapshotIntegrityError(f"Invalid snapshot root hash: {root_hash!r}")
    return store / "manifests" / f"{root_hash}.json"


def _read_manifest_json(path: Path) -> object:
    """Read one bounded regular manifest without following a symlink."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not stat snapshot manifest: {exc}"
        ) from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SnapshotIntegrityError(
            "Snapshot manifest must be an unredirected regular file."
        )
    if before.st_size > MAX_MANIFEST_BYTES:
        raise SnapshotIntegrityError("Snapshot manifest exceeds the read limit.")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not securely open snapshot manifest: {exc}"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
            ):
                raise SnapshotIntegrityError(
                    "Snapshot manifest changed while being opened."
                )
            payload = handle.read(MAX_MANIFEST_BYTES + 1)
            after = os.fstat(handle.fileno())
            if (
                len(payload) > MAX_MANIFEST_BYTES
                or _stat_signature(opened) != _stat_signature(after)
            ):
                raise SnapshotIntegrityError(
                    "Snapshot manifest changed while being read."
                )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(
            f"Could not read snapshot manifest: {exc}"
        ) from exc


def _read_bounded_json_object(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> dict[str, object]:
    """Read one stable regular JSON object without following a link."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise SnapshotIntegrityError(f"Could not inspect {label}: {exc}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size <= 0
        or before.st_size > max_bytes
    ):
        raise SnapshotIntegrityError(
            f"{label} must be a bounded, unredirected regular file."
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SnapshotIntegrityError(f"Could not securely open {label}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not os.path.samestat(before, opened)
            ):
                raise SnapshotIntegrityError(f"{label} changed while opening.")
            payload = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
            if (
                len(payload) > max_bytes
                or _stat_signature(opened) != _stat_signature(after)
            ):
                raise SnapshotIntegrityError(f"{label} changed while reading.")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(f"Could not read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise SnapshotIntegrityError(f"{label} must be a JSON object.")
    return value


def _validate_release_sequence_floor(
    value: Mapping[str, object],
    *,
    label: str,
) -> tuple[int, str]:
    """Return the authenticated release identity encoded by a floor record."""

    sequence = value.get("sequence")
    tree_sha256 = value.get("tree_sha256")
    recorded_at = value.get("recorded_at")
    if (
        set(value) != {"schema", "sequence", "tree_sha256", "recorded_at"}
        or value.get("schema") != 1
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence <= 0
        or not isinstance(tree_sha256, str)
        or not _DIGEST_RE.fullmatch(tree_sha256)
        or not isinstance(recorded_at, (int, float))
        or isinstance(recorded_at, bool)
        or not math.isfinite(float(recorded_at))
        or float(recorded_at) <= 0
    ):
        raise SnapshotIntegrityError(f"{label} is invalid.")
    return sequence, tree_sha256


def _read_local_release_sequence_floor(root: Path) -> tuple[int, str] | None:
    path = root / RELEASE_SEQUENCE_FLOOR
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not inspect release sequence floor: {exc}"
        ) from exc
    value = _read_bounded_json_object(
        path,
        max_bytes=MAX_RELEASE_SEQUENCE_FLOOR_BYTES,
        label="release sequence floor",
    )
    return _validate_release_sequence_floor(
        value,
        label="Release sequence floor",
    )


def _read_snapshot_release_sequence_floor(
    store: Path,
    entry: Mapping[str, object],
    *,
    chunk_size: int,
) -> tuple[int, str]:
    size = int(entry["size"])
    if size <= 0 or size > MAX_RELEASE_SEQUENCE_FLOOR_BYTES:
        raise SnapshotIntegrityError(
            "Snapshot release sequence floor exceeds its safety bound."
        )
    with _open_verified_object(
        store,
        str(entry["sha256"]),
        size,
        chunk_size,
    ) as handle:
        payload = handle.read(MAX_RELEASE_SEQUENCE_FLOOR_BYTES + 1)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(
            f"Snapshot release sequence floor is corrupt: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise SnapshotIntegrityError(
            "Snapshot release sequence floor must be a JSON object."
        )
    return _validate_release_sequence_floor(
        value,
        label="Snapshot release sequence floor",
    )


def _plan_release_sequence_floor_restore(
    root: Path,
    store: Path,
    manifest: Mapping[str, object],
    *,
    chunk_size: int,
) -> _ReleaseFloorPlan | None:
    current = _read_local_release_sequence_floor(root)
    snapshot_entry = next(
        (
            entry
            for entry in manifest["files"]  # type: ignore[union-attr]
            if str(entry["path"]) == RELEASE_SEQUENCE_FLOOR
        ),
        None,
    )
    snapshot = (
        _read_snapshot_release_sequence_floor(
            store,
            snapshot_entry,
            chunk_size=chunk_size,
        )
        if snapshot_entry is not None
        else None
    )
    if current is None and snapshot is None:
        return None
    if (
        current is not None
        and snapshot is not None
        and current[0] == snapshot[0]
        and current[1] != snapshot[1]
    ):
        raise SnapshotIntegrityError(
            "Release sequence floor reuses one sequence for different "
            "immutable release trees."
        )
    minimum = current
    if minimum is None or (
        snapshot is not None and snapshot[0] > minimum[0]
    ):
        minimum = snapshot
    assert minimum is not None
    return _ReleaseFloorPlan(
        snapshot_entry=snapshot_entry,
        snapshot_sequence=snapshot[0] if snapshot is not None else None,
        snapshot_tree_sha256=snapshot[1] if snapshot is not None else None,
        minimum_sequence=minimum[0],
        minimum_tree_sha256=minimum[1],
    )


def _restore_release_sequence_floor(
    root: Path,
    store: Path,
    plan: _ReleaseFloorPlan,
    *,
    root_hash: str,
    chunk_size: int,
) -> None:
    if plan.snapshot_entry is None:
        return
    assert plan.snapshot_sequence is not None
    assert plan.snapshot_tree_sha256 is not None
    current = _read_local_release_sequence_floor(root)
    if current is not None:
        if (
            current[0] == plan.snapshot_sequence
            and current[1] != plan.snapshot_tree_sha256
        ):
            raise SnapshotIntegrityError(
                "Release sequence floor reuses one sequence for different "
                "immutable release trees."
            )
        if current[0] >= plan.snapshot_sequence:
            return
    _restore_manifest_file(
        root,
        store,
        plan.snapshot_entry,
        root_hash=root_hash,
        chunk_size=chunk_size,
    )


def _verify_release_sequence_floor(
    root: Path,
    plan: _ReleaseFloorPlan,
) -> None:
    current = _read_local_release_sequence_floor(root)
    if current is None or current[0] < plan.minimum_sequence:
        raise SnapshotIntegrityError(
            "In-place restore lowered or removed the release sequence floor."
        )
    if (
        current[0] == plan.minimum_sequence
        and current[1] != plan.minimum_tree_sha256
    ):
        raise SnapshotIntegrityError(
            "In-place restore changed the immutable release tree at the "
            "release sequence floor."
        )


def _load_manifest(
    snapshot: Mapping[str, object] | str | Path,
    store: Path,
) -> dict[str, object]:
    if isinstance(snapshot, Mapping):
        return _validate_manifest(dict(snapshot))
    value = Path(snapshot) if isinstance(snapshot, Path) else snapshot
    if isinstance(value, str) and _DIGEST_RE.fullmatch(value):
        path = _manifest_path(store, value)
    else:
        path = Path(value)
    raw = _read_manifest_json(path)
    manifest = _validate_manifest(raw)
    if path.name.endswith(".json") and _DIGEST_RE.fullmatch(path.stem):
        if path.stem != manifest["root_hash"]:
            raise SnapshotIntegrityError("Manifest filename and root hash disagree.")
    return manifest


def create_local_snapshot(
    root: Path,
    *,
    release_id: str,
    policy: data_policy.DataPolicy | None = None,
    store: Path | None = None,
    previous_manifest: Mapping[str, object] | str | Path | None = None,
    limits: SnapshotLimits | None = None,
) -> SnapshotResult:
    """Create and fully verify one deterministic content-addressed snapshot."""

    root = Path(root).resolve(strict=True)
    with _snapshot_store_lock(root):
        return _create_local_snapshot_locked(
            root,
            release_id=release_id,
            policy=policy,
            store=store,
            previous_manifest=previous_manifest,
            limits=limits,
        )


def _create_local_snapshot_locked(
    root: Path,
    *,
    release_id: str,
    policy: data_policy.DataPolicy | None,
    store: Path | None,
    previous_manifest: Mapping[str, object] | str | Path | None,
    limits: SnapshotLimits | None,
) -> SnapshotResult:
    """Create a snapshot while the instance snapshot-store lock is held."""

    root = Path(root).resolve(strict=True)
    store = _prepare_store(root, store)
    limits = limits or SnapshotLimits()
    limits.validate()
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    policy = policy or load_policy(root)
    protected = policy.resolve(root)
    if len(protected) > limits.max_files:
        raise SnapshotLimitError("Protected data exceeds max_files.")

    entries: list[dict[str, object]] = []
    total_size = 0
    for item in protected:
        digest, size, mode = _copy_source_to_object(root, item, store, limits)
        if item.relative_path == RELEASE_SEQUENCE_FLOOR:
            _read_snapshot_release_sequence_floor(
                store,
                {
                    "sha256": digest,
                    "size": size,
                },
                chunk_size=limits.chunk_size,
            )
        total_size += size
        if total_size > limits.max_total_size:
            raise SnapshotLimitError("Protected data exceeds max_total_size.")
        entries.append(
            {
                "path": item.relative_path,
                "sha256": digest,
                "size": size,
                "mode": mode,
                "classes": list(item.classes),
            }
        )

    previous_paths: set[str] = set()
    if previous_manifest is not None:
        previous = _load_manifest(previous_manifest, store)
        previous_paths = {
            str(entry["path"])
            for entry in previous["files"]  # type: ignore[index]
        }
    current_paths = {str(entry["path"]) for entry in entries}
    body = _manifest_body(
        release_id=release_id,
        files=entries,
        tombstones=previous_paths - current_paths,
    )
    root_hash = hashlib.sha256(_canonical_json(body)).hexdigest()
    manifest = _validate_manifest({**body, "root_hash": root_hash})
    manifest_path = _manifest_path(store, root_hash)
    manifest_bytes = _canonical_json(manifest) + b"\n"
    if manifest_path.exists() or manifest_path.is_symlink():
        existing = _load_manifest(manifest_path, store)
        if _canonical_json(existing) != _canonical_json(manifest):
            raise SnapshotIntegrityError(
                f"Existing manifest {root_hash} has different content."
            )
    else:
        _atomic_write(manifest_path, manifest_bytes, mode=0o400)
    verify_local_snapshot(manifest, store=store, limits=limits)
    return SnapshotResult(
        root_hash=root_hash,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def verify_local_snapshot(
    snapshot: Mapping[str, object] | str | Path,
    *,
    store: Path,
    limits: SnapshotLimits | None = None,
) -> dict[str, object]:
    """Verify canonical metadata and every referenced object byte-for-byte."""

    store = Path(store).resolve()
    limits = limits or SnapshotLimits()
    limits.validate()
    manifest = _load_manifest(snapshot, store)
    files = manifest["files"]
    if len(files) > limits.max_files:  # type: ignore[arg-type]
        raise SnapshotLimitError("Snapshot manifest exceeds max_files.")
    total = 0
    for entry in files:  # type: ignore[union-attr]
        size = int(entry["size"])
        if size > limits.max_file_size:
            raise SnapshotLimitError(f"{entry['path']} exceeds max_file_size.")
        total += size
        if total > limits.max_total_size:
            raise SnapshotLimitError("Snapshot manifest exceeds max_total_size.")
        _verify_object(store, str(entry["sha256"]), size, limits.chunk_size)
        if str(entry["path"]) == RELEASE_SEQUENCE_FLOOR:
            _read_snapshot_release_sequence_floor(
                store,
                entry,
                chunk_size=limits.chunk_size,
            )
    return manifest


def _add_checkpoint_root(
    protected: set[str],
    value: object,
    *,
    label: str,
    required: bool = False,
) -> None:
    if value is None and not required:
        return
    if not isinstance(value, Mapping):
        raise SnapshotIntegrityError(f"{label} must be a JSON object.")
    root_hash = value.get("root_hash")
    if root_hash is None and not required:
        return
    root_hash = str(root_hash or "")
    if not _DIGEST_RE.fullmatch(root_hash):
        raise SnapshotIntegrityError(f"{label} has an invalid snapshot root hash.")
    protected.add(root_hash)


def _snapshot_roots_from_update_journal(
    path: Path,
) -> set[str]:
    value = _read_bounded_json_object(
        path,
        max_bytes=MAX_UPDATE_JOURNAL_BYTES,
        label=f"update transaction journal {path.name}",
    )
    expected = {
        "schema",
        "transaction_id",
        "state",
        "created_at",
        "updated_at",
        "metadata",
        "events",
    }
    transaction_id = value.get("transaction_id")
    if (
        set(value) != expected
        or value.get("schema") != 1
        or not isinstance(transaction_id, str)
        or not _UPDATE_TRANSACTION_ID_RE.fullmatch(transaction_id)
        or transaction_id != path.stem
        or value.get("state") not in _UPDATE_STATES
        or not isinstance(value.get("metadata"), dict)
        or not isinstance(value.get("events"), list)
        or not value["events"]
    ):
        raise SnapshotIntegrityError(
            f"Update transaction journal {path.name} has invalid fields."
        )
    events = value["events"]
    if (
        not all(
            isinstance(event, dict)
            and set(event) == {"state", "at", "detail"}
            and event.get("state") in _UPDATE_STATES
            and isinstance(event.get("detail"), str)
            for event in events
        )
        or events[0].get("state") != "CREATED"
        or events[-1].get("state") != value.get("state")
    ):
        raise SnapshotIntegrityError(
            f"Update transaction journal {path.name} has an invalid event history."
        )

    metadata = value["metadata"]
    protected: set[str] = set()
    if "recovery_checkpoint" in metadata:
        _add_checkpoint_root(
            protected,
            metadata.get("recovery_checkpoint"),
            label=f"{path.name} recovery checkpoint",
            required=True,
        )
    if "checkpoint_recovery" in metadata:
        _add_checkpoint_root(
            protected,
            metadata.get("checkpoint_recovery"),
            label=f"{path.name} checkpoint recovery marker",
            required=True,
        )
    if "candidate_era_preservation" in metadata:
        candidate = metadata.get("candidate_era_preservation")
        if not isinstance(candidate, dict):
            raise SnapshotIntegrityError(
                f"{path.name} candidate-era preservation must be an object."
            )
        state = candidate.get("state")
        if state not in {
            "capturing",
            "failed",
            "verified",
            "not_available",
        }:
            raise SnapshotIntegrityError(
                f"{path.name} candidate-era preservation has an invalid state."
            )
        if "checkpoint" in candidate or state == "verified":
            _add_checkpoint_root(
                protected,
                candidate.get("checkpoint"),
                label=f"{path.name} candidate-era checkpoint",
                required=True,
            )
    return protected


def discover_protected_snapshot_roots(root: Path) -> tuple[str, ...]:
    """Discover every durable checkpoint that canonical GC must retain.

    Update journals are authoritative crash/rollback state. Both incomplete
    and terminal journals remain references until journal retention explicitly
    removes them, so snapshot GC never guesses that a checkpoint is obsolete.
    The in-place restore journal and its last verified result are also retained
    to keep an interrupted or operator-requested restore replayable.
    """

    root = Path(root).resolve(strict=True)
    protected: set[str] = set()
    transaction_root = root / ".silicon" / "transactions"
    if transaction_root.exists() or transaction_root.is_symlink():
        try:
            metadata = transaction_root.lstat()
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"Could not inspect update transaction journals: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SnapshotIntegrityError(
                "Update transaction journal storage must be a real directory."
            )
        for entry in sorted(os.scandir(transaction_root), key=lambda item: item.name):
            if not entry.name.endswith(".json"):
                continue
            protected.update(
                _snapshot_roots_from_update_journal(
                    transaction_root / entry.name
                )
            )

    for relative, label in (
        (IN_PLACE_RESTORE_JOURNAL, "in-place restore journal"),
        (IN_PLACE_RESTORE_LATEST, "last restored snapshot reference"),
    ):
        path = root / relative
        if not path.exists() and not path.is_symlink():
            continue
        value = _read_bounded_json_object(
            path,
            max_bytes=MAX_RESTORE_JOURNAL_BYTES,
            label=label,
        )
        if value.get("schema") != 1:
            raise SnapshotIntegrityError(f"{label} has an unsupported schema.")
        root_hash = str(value.get("root_hash") or "")
        if relative == IN_PLACE_RESTORE_JOURNAL:
            state = value.get("state")
            if state not in {"APPLYING", "COMMITTED"}:
                raise SnapshotIntegrityError(
                    "in-place restore journal has an invalid state."
                )
            if state == "COMMITTED" and value.get(
                "verified_root_hash"
            ) != root_hash:
                raise SnapshotIntegrityError(
                    "committed in-place restore journal is not verified."
                )
        elif value.get("verified_root_hash") != root_hash:
            raise SnapshotIntegrityError(
                "last restored snapshot reference is not verified."
            )
        _add_checkpoint_root(
            protected,
            value,
            label=label,
            required=True,
        )
    return tuple(sorted(protected))


def _normalise_gc_parameters(
    retain_latest: int,
    protected_root_hashes: Iterable[str],
    gc_limits: SnapshotGCLimits | None,
) -> tuple[int, tuple[str, ...], SnapshotGCLimits]:
    if (
        not isinstance(retain_latest, int)
        or isinstance(retain_latest, bool)
        or retain_latest < 0
    ):
        raise ValueError("retain_latest must be a non-negative integer.")
    protected: set[str] = set()
    for value in protected_root_hashes:
        root_hash = str(value)
        if not _DIGEST_RE.fullmatch(root_hash):
            raise SnapshotIntegrityError(
                f"Invalid protected snapshot root hash: {root_hash!r}"
            )
        protected.add(root_hash)
    limits = gc_limits or SnapshotGCLimits()
    limits.validate()
    return retain_latest, tuple(sorted(protected)), limits


def _canonical_store_if_present(root: Path) -> tuple[Path, bool]:
    """Return only the in-instance store, rejecting every redirected component."""

    state_root = root / ".silicon"
    try:
        state_metadata = state_root.lstat()
    except FileNotFoundError:
        return state_root / "snapshots", False
    except OSError as exc:
        raise SnapshotIntegrityError(f"Could not inspect .silicon: {exc}") from exc
    if stat.S_ISLNK(state_metadata.st_mode) or not stat.S_ISDIR(
        state_metadata.st_mode
    ):
        raise data_policy.UnsafePathError(
            "The in-instance .silicon state directory must be a real directory."
        )

    store = state_root / "snapshots"
    try:
        store_metadata = store.lstat()
    except FileNotFoundError:
        return store, False
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not inspect the snapshot store: {exc}"
        ) from exc
    if stat.S_ISLNK(store_metadata.st_mode) or not stat.S_ISDIR(
        store_metadata.st_mode
    ):
        raise data_policy.UnsafePathError(
            "The in-instance snapshot store must be a real directory."
        )
    return store, True


def _require_store_subdirectory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not inspect snapshot directory {path.name}: {exc}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SnapshotIntegrityError(
            f"Snapshot path {path.name} must be a real directory."
        )
    return True


def _scan_manifest_records(
    store: Path,
    limits: SnapshotGCLimits,
    unexpected: list[Path],
) -> tuple[_ManifestRecord, ...]:
    directory = store / "manifests"
    if not _require_store_subdirectory(directory):
        return ()
    records: list[_ManifestRecord] = []
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not enumerate snapshot manifests: {exc}"
        ) from exc
    for entry in entries:
        path = directory / entry.name
        match = _MANIFEST_FILE_RE.fullmatch(entry.name)
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"Could not inspect snapshot manifest entry: {path}"
            ) from exc
        if match and stat.S_ISREG(metadata.st_mode):
            records.append(
                _ManifestRecord(
                    root_hash=match.group(1),
                    path=path,
                    mtime_ns=metadata.st_mtime_ns,
                    size=metadata.st_size,
                )
            )
            if len(records) > limits.max_manifests:
                raise SnapshotLimitError("Snapshot GC exceeds max_manifests.")
            continue
        unexpected.append(path)
        if len(unexpected) > limits.max_unexpected_entries:
            raise SnapshotLimitError(
                "Snapshot GC exceeds max_unexpected_entries."
            )
    return tuple(records)


def _scan_object_records(
    store: Path,
    limits: SnapshotGCLimits,
    unexpected: list[Path],
) -> tuple[tuple[str, Path, int], ...]:
    objects = store / "objects"
    if not _require_store_subdirectory(objects):
        return ()
    sha_directory = objects / "sha256"
    if not _require_store_subdirectory(sha_directory):
        for entry in sorted(os.scandir(objects), key=lambda item: item.name):
            unexpected.append(objects / entry.name)
            if len(unexpected) > limits.max_unexpected_entries:
                raise SnapshotLimitError(
                    "Snapshot GC exceeds max_unexpected_entries."
                )
        return ()

    records: list[tuple[str, Path, int]] = []
    for prefix_entry in sorted(
        os.scandir(sha_directory), key=lambda entry: entry.name
    ):
        prefix_path = sha_directory / prefix_entry.name
        try:
            prefix_metadata = prefix_entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"Could not inspect snapshot object prefix: {prefix_path}"
            ) from exc
        if not (
            _OBJECT_PREFIX_RE.fullmatch(prefix_entry.name)
            and stat.S_ISDIR(prefix_metadata.st_mode)
            and not stat.S_ISLNK(prefix_metadata.st_mode)
        ):
            unexpected.append(prefix_path)
            if len(unexpected) > limits.max_unexpected_entries:
                raise SnapshotLimitError(
                    "Snapshot GC exceeds max_unexpected_entries."
                )
            continue
        for object_entry in sorted(
            os.scandir(prefix_path), key=lambda entry: entry.name
        ):
            object_path = prefix_path / object_entry.name
            try:
                object_metadata = object_entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SnapshotIntegrityError(
                    f"Could not inspect snapshot object: {object_path}"
                ) from exc
            if (
                _OBJECT_SUFFIX_RE.fullmatch(object_entry.name)
                and stat.S_ISREG(object_metadata.st_mode)
            ):
                records.append(
                    (
                        prefix_entry.name + object_entry.name,
                        object_path,
                        object_metadata.st_size,
                    )
                )
                if len(records) > limits.max_objects:
                    raise SnapshotLimitError("Snapshot GC exceeds max_objects.")
                continue
            unexpected.append(object_path)
            if len(unexpected) > limits.max_unexpected_entries:
                raise SnapshotLimitError(
                    "Snapshot GC exceeds max_unexpected_entries."
                )

    for entry in sorted(os.scandir(objects), key=lambda item: item.name):
        if entry.name == "sha256":
            continue
        path = objects / entry.name
        if entry.name == ".tmp":
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SnapshotIntegrityError(
                    f"Could not inspect snapshot temporary directory: {exc}"
                ) from exc
            if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(
                metadata.st_mode
            ):
                temporary_entries = sorted(
                    os.scandir(path),
                    key=lambda item: item.name,
                )
                if not temporary_entries:
                    continue
                for temporary in temporary_entries:
                    unexpected.append(path / temporary.name)
                    if len(unexpected) > limits.max_unexpected_entries:
                        raise SnapshotLimitError(
                            "Snapshot GC exceeds max_unexpected_entries."
                        )
                continue
        if path not in unexpected:
            unexpected.append(path)
            if len(unexpected) > limits.max_unexpected_entries:
                raise SnapshotLimitError(
                    "Snapshot GC exceeds max_unexpected_entries."
                )
    return tuple(records)


def _verify_retained_manifests(
    manifests: Mapping[str, Mapping[str, object]],
    store: Path,
    limits: SnapshotLimits,
) -> set[str]:
    referenced: dict[str, int] = {}
    for root_hash in sorted(manifests):
        manifest = manifests[root_hash]
        files = manifest["files"]
        if len(files) > limits.max_files:  # type: ignore[arg-type]
            raise SnapshotLimitError(
                f"Retained snapshot {root_hash} exceeds max_files."
            )
        total = 0
        for entry in files:  # type: ignore[union-attr]
            size = int(entry["size"])
            if size > limits.max_file_size:
                raise SnapshotLimitError(
                    f"Retained snapshot file {entry['path']} exceeds max_file_size."
                )
            total += size
            if total > limits.max_total_size:
                raise SnapshotLimitError(
                    f"Retained snapshot {root_hash} exceeds max_total_size."
                )
            digest = str(entry["sha256"])
            previous_size = referenced.setdefault(digest, size)
            if previous_size != size:
                raise SnapshotIntegrityError(
                    f"Retained manifests disagree about object {digest}."
                )
    for digest in sorted(referenced):
        _verify_object(store, digest, referenced[digest], limits.chunk_size)
    return set(referenced)


def _empty_gc_plan(
    store: Path,
    retain_latest: int,
    protected_root_hashes: tuple[str, ...],
) -> SnapshotGCPlan:
    if protected_root_hashes:
        raise SnapshotIntegrityError(
            "A protected snapshot root is absent from the canonical local store."
        )
    return SnapshotGCPlan(
        store=store,
        retain_latest=retain_latest,
        protected_root_hashes=protected_root_hashes,
        retained_root_hashes=(),
        delete_manifests=(),
        delete_objects=(),
        discarded_corrupt_manifests=(),
        unexpected_entries=(),
        reclaimable_bytes=0,
        dry_run=True,
    )


def _plan_snapshot_gc_locked(
    root: Path,
    *,
    retain_latest: int,
    protected_root_hashes: tuple[str, ...],
    limits: SnapshotLimits,
    gc_limits: SnapshotGCLimits,
) -> SnapshotGCPlan:
    store, present = _canonical_store_if_present(root)
    if not present:
        return _empty_gc_plan(store, retain_latest, protected_root_hashes)

    unexpected: list[Path] = []
    records = _scan_manifest_records(store, gc_limits, unexpected)
    newest = sorted(
        records,
        key=lambda record: (-record.mtime_ns, record.root_hash),
    )
    retained_roots = {
        record.root_hash for record in newest[:retain_latest]
    } | set(protected_root_hashes)
    records_by_root = {record.root_hash: record for record in records}
    missing = sorted(set(protected_root_hashes) - set(records_by_root))
    if missing:
        raise SnapshotIntegrityError(
            "Protected snapshot root is absent: " + ", ".join(missing)
        )

    parsed: dict[str, dict[str, object]] = {}
    for record in sorted(records, key=lambda item: item.root_hash):
        try:
            parsed[record.root_hash] = _load_manifest(record.path, store)
        except SnapshotError as exc:
            # An expired manifest can still be the only metadata naming an
            # object needed by an operator. Never infer reachability through
            # corrupt metadata; require repair or explicit removal first.
            raise SnapshotIntegrityError(
                f"Snapshot {record.root_hash} is corrupt: {exc}"
            ) from exc

    retained_manifests = {
        root_hash: parsed[root_hash] for root_hash in sorted(retained_roots)
    }
    referenced_objects = _verify_retained_manifests(
        retained_manifests,
        store,
        limits,
    )
    object_records = _scan_object_records(store, gc_limits, unexpected)

    delete_manifests = tuple(
        record.path
        for record in sorted(records, key=lambda item: item.path.name)
        if record.root_hash not in retained_roots
    )
    delete_objects = tuple(
        path
        for digest, path, _size in object_records
        if digest not in referenced_objects
    )
    reclaimable_bytes = sum(
        records_by_root[path.stem].size for path in delete_manifests
    ) + sum(
        size
        for digest, _path, size in object_records
        if digest not in referenced_objects
    )
    return SnapshotGCPlan(
        store=store,
        retain_latest=retain_latest,
        protected_root_hashes=protected_root_hashes,
        retained_root_hashes=tuple(sorted(retained_roots)),
        delete_manifests=delete_manifests,
        delete_objects=delete_objects,
        discarded_corrupt_manifests=(),
        unexpected_entries=tuple(sorted(unexpected)),
        reclaimable_bytes=reclaimable_bytes,
        dry_run=True,
    )


def plan_snapshot_gc(
    root: Path,
    *,
    retain_latest: int = DEFAULT_SNAPSHOT_RETENTION,
    protected_root_hashes: Iterable[str] = (),
    limits: SnapshotLimits | None = None,
    gc_limits: SnapshotGCLimits | None = None,
) -> SnapshotGCPlan:
    """Verify and return a deterministic, no-delete canonical-store GC plan."""

    root = Path(root).resolve(strict=True)
    retain_latest, protected, gc_limits = _normalise_gc_parameters(
        retain_latest,
        protected_root_hashes,
        gc_limits,
    )
    limits = limits or SnapshotLimits()
    limits.validate()
    _store, present = _canonical_store_if_present(root)
    if not present:
        return _empty_gc_plan(_store, retain_latest, protected)
    with _snapshot_store_lock(root):
        return _plan_snapshot_gc_locked(
            root,
            retain_latest=retain_latest,
            protected_root_hashes=protected,
            limits=limits,
            gc_limits=gc_limits,
        )


def _assert_canonical_delete_path(path: Path, store: Path, kind: str) -> None:
    if kind == "manifest":
        expected_parent = store / "manifests"
        valid_name = _MANIFEST_FILE_RE.fullmatch(path.name) is not None
    elif kind == "object":
        expected_parent = store / "objects" / "sha256" / path.parent.name
        valid_name = bool(
            _OBJECT_PREFIX_RE.fullmatch(path.parent.name)
            and _OBJECT_SUFFIX_RE.fullmatch(path.name)
        )
    else:  # pragma: no cover - internal contract
        raise AssertionError(f"Unknown snapshot deletion kind: {kind}")
    if path.parent != expected_parent or not valid_name:
        raise SnapshotIntegrityError(
            f"Refusing non-canonical snapshot {kind} deletion: {path}"
        )
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Snapshot {kind} changed before deletion: {path}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SnapshotIntegrityError(
            f"Refusing redirected/non-regular snapshot {kind}: {path}"
        )


def _apply_snapshot_gc_plan(plan: SnapshotGCPlan) -> SnapshotGCPlan:
    if plan.unexpected_entries:
        raise SnapshotIntegrityError(
            "Snapshot GC found unexpected or unsafe entries; refusing every "
            "deletion: "
            + ", ".join(str(path) for path in plan.unexpected_entries[:10])
        )
    for path in plan.delete_manifests:
        _assert_canonical_delete_path(path, plan.store, "manifest")
    for path in plan.delete_objects:
        _assert_canonical_delete_path(path, plan.store, "object")

    modified_directories: set[Path] = set()
    for path in plan.delete_manifests:
        path.unlink()
        modified_directories.add(path.parent)
    for directory in sorted(modified_directories):
        _fsync_directory(directory)

    modified_directories.clear()
    for path in plan.delete_objects:
        path.unlink()
        modified_directories.add(path.parent)
    for directory in sorted(modified_directories):
        _fsync_directory(directory)
    return SnapshotGCPlan(
        store=plan.store,
        retain_latest=plan.retain_latest,
        protected_root_hashes=plan.protected_root_hashes,
        retained_root_hashes=plan.retained_root_hashes,
        delete_manifests=plan.delete_manifests,
        delete_objects=plan.delete_objects,
        discarded_corrupt_manifests=plan.discarded_corrupt_manifests,
        unexpected_entries=plan.unexpected_entries,
        reclaimable_bytes=plan.reclaimable_bytes,
        dry_run=False,
    )


def garbage_collect_snapshots(
    root: Path,
    *,
    retain_latest: int = DEFAULT_SNAPSHOT_RETENTION,
    protected_root_hashes: Iterable[str] = (),
    dry_run: bool = False,
    limits: SnapshotLimits | None = None,
    gc_limits: SnapshotGCLimits | None = None,
) -> SnapshotGCPlan:
    """Collect only unreferenced canonical files under ``.silicon/snapshots``."""

    root = Path(root).resolve(strict=True)
    retain_latest, protected, gc_limits = _normalise_gc_parameters(
        retain_latest,
        protected_root_hashes,
        gc_limits,
    )
    limits = limits or SnapshotLimits()
    limits.validate()
    store, present = _canonical_store_if_present(root)
    if not present:
        return _empty_gc_plan(store, retain_latest, protected)
    with _snapshot_store_lock(root):
        plan = _plan_snapshot_gc_locked(
            root,
            retain_latest=retain_latest,
            protected_root_hashes=protected,
            limits=limits,
            gc_limits=gc_limits,
        )
        if dry_run:
            return plan
        return _apply_snapshot_gc_plan(plan)


def garbage_collect_referenced_snapshots(
    root: Path,
    *,
    retain_latest: int = DEFAULT_SNAPSHOT_RETENTION,
    additional_protected_root_hashes: Iterable[str] = (),
    dry_run: bool = False,
    limits: SnapshotLimits | None = None,
    gc_limits: SnapshotGCLimits | None = None,
) -> SnapshotGCPlan:
    """Discover durable references and collect under one snapshot-store lock."""

    root = Path(root).resolve(strict=True)
    retain_latest, additional, gc_limits = _normalise_gc_parameters(
        retain_latest,
        additional_protected_root_hashes,
        gc_limits,
    )
    limits = limits or SnapshotLimits()
    limits.validate()
    with _snapshot_store_lock(root):
        protected = tuple(
            sorted(
                {
                    *additional,
                    *discover_protected_snapshot_roots(root),
                }
            )
        )
        store, present = _canonical_store_if_present(root)
        if not present:
            return _empty_gc_plan(store, retain_latest, protected)
        plan = _plan_snapshot_gc_locked(
            root,
            retain_latest=retain_latest,
            protected_root_hashes=protected,
            limits=limits,
            gc_limits=gc_limits,
        )
        if dry_run:
            return plan
        return _apply_snapshot_gc_plan(plan)


def plan_restore(
    snapshot: Mapping[str, object] | str | Path,
    target: Path,
    *,
    store: Path,
    limits: SnapshotLimits | None = None,
) -> RestorePlan:
    """Verify a snapshot and return the exact no-write restore plan."""

    target = Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise SnapshotError("Restore target must be a new, nonexistent directory.")
    manifest = verify_local_snapshot(snapshot, store=store, limits=limits)
    files = tuple(str(entry["path"]) for entry in manifest["files"])  # type: ignore[index]
    total = sum(int(entry["size"]) for entry in manifest["files"])  # type: ignore[index]
    return RestorePlan(
        root_hash=str(manifest["root_hash"]),
        release_id=str(manifest["release_id"]),
        target=target,
        files=files,
        tombstones=tuple(str(item) for item in manifest["tombstones"]),  # type: ignore[arg-type]
        total_size=total,
        dry_run=True,
    )


def restore_snapshot(
    snapshot: Mapping[str, object] | str | Path,
    target: Path,
    *,
    store: Path,
    dry_run: bool = False,
    limits: SnapshotLimits | None = None,
) -> RestorePlan:
    """Restore into a new directory through an atomic sibling staging path."""

    plan = plan_restore(snapshot, target, store=store, limits=limits)
    if dry_run:
        return plan
    manifest = _load_manifest(snapshot, Path(store).resolve())
    target = plan.target
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.restore.", dir=target.parent)
    )
    committed = False
    try:
        for entry in manifest["files"]:  # type: ignore[union-attr]
            relative = data_policy.validate_relative_path(str(entry["path"]))
            destination = staging.joinpath(*Path(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = destination.with_name(f".{destination.name}.partial")
            with (
                _open_verified_object(
                    Path(store).resolve(),
                    str(entry["sha256"]),
                    int(entry["size"]),
                    (limits or SnapshotLimits()).chunk_size,
                ) as reader,
                temporary.open("xb") as writer,
            ):
                shutil.copyfileobj(
                    reader,
                    writer,
                    length=(limits or SnapshotLimits()).chunk_size,
                )
                writer.flush()
                os.fsync(writer.fileno())
            os.chmod(temporary, int(entry["mode"]) & 0o777)
            os.replace(temporary, destination)
            digest, size = _hash_file(
                destination,
                (limits or SnapshotLimits()).chunk_size,
            )
            if digest != entry["sha256"] or size != entry["size"]:
                raise SnapshotIntegrityError(
                    f"Restored file failed verification: {relative}"
                )
        if target.exists() or target.is_symlink():
            raise SnapshotError("Restore target appeared during restore.")
        os.replace(staging, target)
        committed = True
        _fsync_directory(target.parent)
    finally:
        if not committed:
            shutil.rmtree(staging, ignore_errors=True)
    return RestorePlan(
        root_hash=plan.root_hash,
        release_id=plan.release_id,
        target=plan.target,
        files=plan.files,
        tombstones=plan.tombstones,
        total_size=plan.total_size,
        dry_run=False,
    )


def _validate_in_place_restore_path(relative: str) -> str:
    """Reject operational state that a protected-data restore must never own."""

    value = data_policy.validate_relative_path(relative)
    if value.startswith(".silicon/") and not (
        value == ".silicon/data-policy.json"
        or value == RELEASE_SEQUENCE_FLOOR
        or value.startswith(".silicon/overlays/")
    ):
        raise SnapshotIntegrityError(
            f"Snapshot attempts to restore updater/runtime state: {value}"
        )
    return value


def _restore_parent(root: Path, relative: str) -> tuple[Path, Path]:
    """Return a confined real parent and destination, creating safe parents."""

    value = _validate_in_place_restore_path(relative)
    destination = root.joinpath(*PurePath(value).parts)
    parent = root
    for component in PurePath(value).parts[:-1]:
        candidate = parent / component
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            try:
                candidate.mkdir(mode=0o700)
                _fsync_directory(parent)
            except FileExistsError:
                pass
            metadata = candidate.lstat()
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"Could not inspect restore parent for {value}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise data_policy.UnsafePathError(
                f"Restore parent must be a real directory: {candidate}"
            )
        parent = candidate
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not inspect restore destination {value}: {exc}"
        ) from exc
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise data_policy.UnsafePathError(
                f"Restore destination must be a regular file: {destination}"
            )
    return parent, destination


def _restore_manifest_file(
    root: Path,
    store: Path,
    entry: Mapping[str, object],
    *,
    root_hash: str,
    chunk_size: int,
) -> None:
    relative = _validate_in_place_restore_path(str(entry["path"]))
    parent, destination = _restore_parent(root, relative)
    temporary = parent / (
        f".{destination.name}.silicon-restore-{root_hash[:16]}.partial"
    )
    reusable_temporary = False
    try:
        temporary_metadata = temporary.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not inspect restore temporary for {relative}: {exc}"
        ) from exc
    else:
        if (
            stat.S_ISLNK(temporary_metadata.st_mode)
            or not stat.S_ISREG(temporary_metadata.st_mode)
        ):
            raise data_policy.UnsafePathError(
                f"Restore temporary must be a regular file: {temporary}"
            )
        digest, size = _hash_file(temporary, chunk_size)
        if digest != entry["sha256"] or size != entry["size"]:
            raise SnapshotIntegrityError(
                f"Refusing to replace an unknown restore temporary: {temporary}"
            )
        reusable_temporary = True

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        if not reusable_temporary:
            descriptor = os.open(temporary, flags, 0o600)
            with (
                _open_verified_object(
                    store,
                    str(entry["sha256"]),
                    int(entry["size"]),
                    chunk_size,
                ) as reader,
                os.fdopen(descriptor, "wb") as writer,
            ):
                descriptor = -1
                shutil.copyfileobj(reader, writer, length=chunk_size)
                writer.flush()
                os.fsync(writer.fileno())
        os.chmod(temporary, int(entry["mode"]) & 0o777)
        # Revalidate the full destination chain immediately before commit.
        checked_parent, checked_destination = _restore_parent(root, relative)
        if checked_parent != parent or checked_destination != destination:
            raise data_policy.UnsafePathError(
                f"Restore destination changed during commit: {relative}"
            )
        os.replace(temporary, destination)
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)

    digest, size = _hash_file(destination, chunk_size)
    if digest != entry["sha256"] or size != entry["size"]:
        raise SnapshotIntegrityError(
            f"In-place restored file failed verification: {relative}"
        )
    if stat.S_IMODE(destination.stat().st_mode) != int(entry["mode"]):
        raise SnapshotIntegrityError(
            f"In-place restored file mode failed verification: {relative}"
        )


def _restore_tombstone_destination(root: Path, relative: str) -> Path | None:
    value = _validate_in_place_restore_path(relative)
    parts = PurePath(value).parts
    parent = root
    for component in parts[:-1]:
        candidate = parent / component
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"Could not inspect restore tombstone parent for {value}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise data_policy.UnsafePathError(
                f"Restore tombstone parent must be a real directory: {candidate}"
            )
        parent = candidate
    return parent / parts[-1]


def _restore_manifest_tombstone(root: Path, relative: str) -> None:
    destination = _restore_tombstone_destination(root, relative)
    if destination is None:
        return
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise data_policy.UnsafePathError(
            f"Refusing to remove a non-regular restore tombstone: {destination}"
        )
    destination.unlink()
    _fsync_directory(destination.parent)


def _read_restore_journal(path: Path) -> dict[str, object] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SnapshotIntegrityError(
            f"Could not inspect in-place restore journal: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > MAX_RESTORE_JOURNAL_BYTES
    ):
        raise SnapshotIntegrityError(
            "The in-place restore journal must be a bounded regular file."
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(
            f"Could not read in-place restore journal: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise SnapshotIntegrityError(
            "The in-place restore journal must be a JSON object."
        )
    return value


def _write_restore_journal(path: Path, value: Mapping[str, object]) -> None:
    _atomic_write(
        path,
        (
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8"),
        mode=0o600,
    )


def _verify_in_place_restore(
    root: Path,
    manifest: Mapping[str, object],
    *,
    chunk_size: int,
    release_floor_plan: _ReleaseFloorPlan | None,
) -> None:
    for entry in manifest["files"]:  # type: ignore[union-attr]
        relative = _validate_in_place_restore_path(str(entry["path"]))
        if relative == RELEASE_SEQUENCE_FLOOR:
            continue
        _parent, destination = _restore_parent(root, relative)
        try:
            metadata = destination.lstat()
        except FileNotFoundError as exc:
            raise SnapshotIntegrityError(
                f"In-place restore is missing {relative}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SnapshotIntegrityError(
                f"In-place restore produced an unsafe path: {relative}"
            )
        digest, size = _hash_file(destination, chunk_size)
        if (
            digest != entry["sha256"]
            or size != entry["size"]
            or stat.S_IMODE(metadata.st_mode) != int(entry["mode"])
        ):
            raise SnapshotIntegrityError(
                f"In-place restore verification failed: {relative}"
            )
    for relative in manifest["tombstones"]:  # type: ignore[union-attr]
        value = _validate_in_place_restore_path(str(relative))
        if value == RELEASE_SEQUENCE_FLOOR:
            continue
        destination = _restore_tombstone_destination(root, value)
        if destination is not None and (
            destination.exists() or destination.is_symlink()
        ):
            raise SnapshotIntegrityError(
                f"In-place restore tombstone still exists: {value}"
            )
    if release_floor_plan is not None:
        _verify_release_sequence_floor(root, release_floor_plan)


def _restore_local_snapshot_in_place_locked(
    root: Path,
    snapshot: Mapping[str, object] | str | Path,
    *,
    store: Path,
    dry_run: bool = False,
    limits: SnapshotLimits | None = None,
) -> RestorePlan:
    """Idempotently restore only manifest-owned protected data in an instance.

    The caller must stop Silicon services first. Unknown post-snapshot files are
    deliberately preserved. Progress is journaled after each atomic file or
    tombstone operation, so calling this function again safely resumes a crash.
    """

    requested_root = Path(root)
    try:
        root_metadata = requested_root.lstat()
    except OSError as exc:
        raise SnapshotError(f"Could not inspect in-place restore root: {exc}") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise data_policy.UnsafePathError(
            "The in-place restore root must be a real directory."
        )
    root = requested_root.resolve(strict=True)
    limits = limits or SnapshotLimits()
    limits.validate()

    with _snapshot_store_lock(root):
        canonical_store, store_present = _canonical_store_if_present(root)
        if not store_present:
            raise SnapshotIntegrityError(
                "The canonical local snapshot store does not exist."
            )
        store = Path(store).resolve(strict=True)
        if store != canonical_store.resolve(strict=True):
            raise data_policy.UnsafePathError(
                "In-place recovery only accepts the canonical in-instance "
                "snapshot store."
            )
        manifest = verify_local_snapshot(snapshot, store=store, limits=limits)
        files = tuple(str(entry["path"]) for entry in manifest["files"])  # type: ignore[index]
        tombstones = tuple(str(item) for item in manifest["tombstones"])  # type: ignore[arg-type]
        for relative in (*files, *tombstones):
            _validate_in_place_restore_path(relative)
        release_floor_plan = _plan_release_sequence_floor_restore(
            root,
            store,
            manifest,
            chunk_size=limits.chunk_size,
        )
        plan = RestorePlan(
            root_hash=str(manifest["root_hash"]),
            release_id=str(manifest["release_id"]),
            target=root,
            files=files,
            tombstones=tombstones,
            total_size=sum(
                int(entry["size"])
                for entry in manifest["files"]  # type: ignore[index]
            ),
            dry_run=dry_run,
        )
        if dry_run:
            return plan

        journal_path = root / IN_PLACE_RESTORE_JOURNAL
        existing = _read_restore_journal(journal_path)
        operation_count = len(files) + len(tombstones)
        if existing:
            state = existing.get("state")
            next_value = existing.get("next_operation")
            created_value = existing.get("created_at")
            if (
                existing.get("schema") != 1
                or state not in {"APPLYING", "COMMITTED"}
                or not _DIGEST_RE.fullmatch(str(existing.get("root_hash") or ""))
                or not str(existing.get("release_id") or "")
                or not isinstance(existing.get("operation_count"), int)
                or isinstance(existing.get("operation_count"), bool)
                or int(existing["operation_count"]) < 0
                or not isinstance(next_value, int)
                or isinstance(next_value, bool)
                or not 0 <= int(next_value) <= int(existing["operation_count"])
                or not isinstance(created_value, (int, float))
                or isinstance(created_value, bool)
                or float(created_value) <= 0
            ):
                raise SnapshotIntegrityError(
                    "The in-place restore journal has invalid fields."
                )
            if state == "COMMITTED" and (
                existing.get("verified_root_hash") != existing.get("root_hash")
                or int(existing["next_operation"])
                != int(existing["operation_count"])
                or not isinstance(existing.get("committed_at"), (int, float))
                or isinstance(existing.get("committed_at"), bool)
                or float(existing["committed_at"]) <= 0
            ):
                raise SnapshotIntegrityError(
                    "The committed in-place restore journal is invalid."
                )
            if existing.get("root_hash") == plan.root_hash and (
                existing.get("release_id") != plan.release_id
                or existing.get("operation_count") != operation_count
            ):
                raise SnapshotIntegrityError(
                    "The in-place restore journal does not match its snapshot."
                )
        if existing and existing.get("state") == "APPLYING":
            if existing.get("root_hash") != plan.root_hash:
                raise SnapshotError(
                    "A different protected-data restore is incomplete; resume "
                    f"{existing.get('root_hash')} before restoring {plan.root_hash}."
                )
            if (
                existing.get("schema") != 1
                or existing.get("release_id") != plan.release_id
                or existing.get("operation_count") != operation_count
            ):
                raise SnapshotIntegrityError(
                    "The in-place restore journal does not match its snapshot."
                )
            next_operation = int(existing["next_operation"])
            # A prior process may have finished all operations but crashed
            # before verification. Reapply once so repair remains deterministic.
            if next_operation == operation_count:
                next_operation = 0
            created_at = float(existing.get("created_at") or time.time())
        else:
            next_operation = 0
            created_at = time.time()

        journal: dict[str, object] = {
            "schema": 1,
            "root_hash": plan.root_hash,
            "release_id": plan.release_id,
            "state": "APPLYING",
            "operation_count": operation_count,
            "next_operation": next_operation,
            "created_at": created_at,
            "updated_at": time.time(),
        }
        _write_restore_journal(journal_path, journal)
        operations: list[tuple[str, object]] = [
            ("file", entry)
            for entry in manifest["files"]  # type: ignore[union-attr]
        ]
        operations.extend(("tombstone", value) for value in tombstones)
        for index in range(next_operation, operation_count):
            kind, payload = operations[index]
            if kind == "file":
                if str(payload["path"]) == RELEASE_SEQUENCE_FLOOR:  # type: ignore[index]
                    if release_floor_plan is None:
                        raise SnapshotIntegrityError(
                            "Release sequence floor restore plan is missing."
                        )
                    _restore_release_sequence_floor(
                        root,
                        store,
                        release_floor_plan,
                        root_hash=plan.root_hash,
                        chunk_size=limits.chunk_size,
                    )
                else:
                    _restore_manifest_file(
                        root,
                        store,
                        payload,  # type: ignore[arg-type]
                        root_hash=plan.root_hash,
                        chunk_size=limits.chunk_size,
                    )
            else:
                if str(payload) != RELEASE_SEQUENCE_FLOOR:
                    _restore_manifest_tombstone(root, str(payload))
            journal["next_operation"] = index + 1
            journal["updated_at"] = time.time()
            _write_restore_journal(journal_path, journal)

        try:
            _verify_in_place_restore(
                root,
                manifest,
                chunk_size=limits.chunk_size,
                release_floor_plan=release_floor_plan,
            )
        except Exception:
            journal["next_operation"] = 0
            journal["updated_at"] = time.time()
            _write_restore_journal(journal_path, journal)
            raise

        journal["state"] = "COMMITTED"
        journal["verified_root_hash"] = plan.root_hash
        journal["committed_at"] = time.time()
        journal["updated_at"] = journal["committed_at"]
        _write_restore_journal(journal_path, journal)
        _write_restore_journal(
            root / IN_PLACE_RESTORE_LATEST,
            {
                "schema": 1,
                "root_hash": plan.root_hash,
                "verified_root_hash": plan.root_hash,
                "release_id": plan.release_id,
                "file_count": len(files),
                "tombstone_count": len(tombstones),
                "committed_at": journal["committed_at"],
            },
        )
        return RestorePlan(
            root_hash=plan.root_hash,
            release_id=plan.release_id,
            target=plan.target,
            files=plan.files,
            tombstones=plan.tombstones,
            total_size=plan.total_size,
            dry_run=False,
        )


def restore_local_snapshot_in_place(
    root: Path,
    snapshot: Mapping[str, object] | str | Path,
    *,
    store: Path,
    dry_run: bool = False,
    limits: SnapshotLimits | None = None,
) -> RestorePlan:
    """Restore protected data while serializing the anti-rollback floor."""

    requested_root = Path(root)
    try:
        root_metadata = requested_root.lstat()
    except OSError as exc:
        raise SnapshotError(f"Could not inspect in-place restore root: {exc}") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise data_policy.UnsafePathError(
            "The in-place restore root must be a real directory."
        )
    canonical_root = requested_root.resolve(strict=True)
    with _release_floor_lock(canonical_root):
        return _restore_local_snapshot_in_place_locked(
            canonical_root,
            snapshot,
            store=store,
            dry_run=dry_run,
            limits=limits,
        )


def _tar_directory_info(relative: str, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(relative.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = mode & 0o777
    info.size = 0
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _tar_file_info(relative: str, mode: int, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(relative)
    info.type = tarfile.REGTYPE
    info.mode = mode & 0o777
    info.size = size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _write_archive(
    spool: BinaryIO,
    entries: Sequence[tuple[str, int, int]],
    open_source: Callable[[str], BinaryIO],
) -> None:
    directories: set[str] = set()
    for relative, _mode, _size in entries:
        parent = PurePath(relative).parent
        while parent.as_posix() not in {"", "."}:
            directories.add(parent.as_posix())
            parent = parent.parent
    with gzip.GzipFile(fileobj=spool, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for directory in sorted(directories):
                archive.addfile(_tar_directory_info(directory, 0o700))
            for relative, mode, size in entries:
                source = open_source(relative)
                try:
                    opened = os.fstat(source.fileno())
                    if not stat.S_ISREG(opened.st_mode) or opened.st_size != size:
                        raise SnapshotIntegrityError(
                            f"Archive source changed before streaming: {relative}"
                        )
                    archive.addfile(
                        _tar_file_info(relative, mode, size),
                        fileobj=source,
                    )
                finally:
                    source.close()


def build_archive_file(
    root: Path,
    patterns: Iterable[str],
    *,
    spool_limit: int = DEFAULT_SPOOL_LIMIT,
) -> tuple[BinaryIO, list[str]]:
    """Safely build the legacy archive, spilling beyond ``spool_limit``."""

    root = Path(root).resolve(strict=True)
    matched: dict[str, Path] = {}
    top_candidates: dict[str, Path] = {}
    for pattern in patterns:
        for path in data_policy.expand_pattern(root, pattern):
            relative = path.relative_to(root).as_posix()
            top_candidates[relative] = path
        for path in data_policy.expand_pattern_files(root, pattern):
            matched[path.relative_to(root).as_posix()] = path
    directories = {
        relative
        for relative, path in top_candidates.items()
        if stat.S_ISDIR(path.lstat().st_mode)
    }
    included = [
        relative
        for relative in sorted(top_candidates)
        if not any(
            relative != directory and relative.startswith(directory + "/")
            for directory in directories
        )
    ]
    entries: list[tuple[str, int, int]] = []
    for relative in sorted(matched):
        source, metadata = _safe_source_open(root, relative)
        source.close()
        entries.append(
            (
                relative,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_size,
            )
        )
    spool = tempfile.SpooledTemporaryFile(max_size=spool_limit, mode="w+b")
    try:
        _write_archive(
            spool,
            entries,
            lambda relative: _safe_source_open(root, relative)[0],
        )
    except Exception:
        spool.close()
        raise
    spool.seek(0)
    return spool, included


def build_archive(root: Path, patterns: list[str]) -> tuple[bytes, list[str]]:
    """Compatibility wrapper returning bytes; new code should stream the file."""

    archive, included = build_archive_file(root, patterns)
    try:
        return archive.read(), included
    finally:
        archive.close()


def _archive_snapshot_file(
    manifest: Mapping[str, object],
    store: Path,
    *,
    spool_limit: int = DEFAULT_SPOOL_LIMIT,
) -> tuple[BinaryIO, list[str]]:
    entries: list[tuple[str, int, int]] = []
    included: list[str] = []
    metadata: dict[str, tuple[str, int]] = {}
    for item in manifest["files"]:  # type: ignore[union-attr]
        relative = str(item["path"])
        digest = str(item["sha256"])
        size = int(item["size"])
        entries.append(
            (
                relative,
                int(item["mode"]),
                size,
            )
        )
        metadata[relative] = (digest, size)
        included.append(relative)
    spool = tempfile.SpooledTemporaryFile(max_size=spool_limit, mode="w+b")
    try:
        _write_archive(
            spool,
            entries,
            lambda relative: _open_verified_object(
                store,
                metadata[relative][0],
                metadata[relative][1],
                CHUNK_SIZE,
            ),
        )
    except Exception:
        spool.close()
        raise
    spool.seek(0)
    return spool, included


def run_backup(
    start: str | os.PathLike | None = None,
    note: str = "on-demand",
    logger=print,
    release_id: str | None = None,
) -> bool:
    """Create a verified local snapshot and upload a disk-spooled archive."""

    root = _instance_root(start)
    policy = load_policy(root)
    snapshot = create_local_snapshot(
        root,
        release_id=release_id or installed_release_id(root),
        policy=policy,
    )
    if not snapshot.manifest["files"]:
        logger("backup: protected-data policy matched no files")
        return False

    store = root / data_policy.SNAPSHOT_STORE
    archive, included = _archive_snapshot_file(snapshot.manifest, store)
    if not included:
        archive.close()
        logger("backup: protected-data policy matched no files")
        return False

    config = _load_glass_config(root)
    if not str(config.get("api_key") or config.get("silicon_api_key") or "").strip():
        archive.close()
        raise ValueError(".glass.json does not contain an api_key")

    try:
        response = silicon_api_request(
            "POST",
            UPLOAD_PATH,
            config=config,
            files={"file": ("backup.tar.gz", archive, "application/gzip")},
            form_data={
                "manifest": json.dumps(included),
                "snapshot_manifest": json.dumps(
                    snapshot.manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "root_hash": snapshot.root_hash,
                "release_id": snapshot.manifest["release_id"],
                "note": note,
            },
            timeout=UPLOAD_TIMEOUT,
        )
    finally:
        archive.close()
    if response.status_code in {200, 201}:
        try:
            seq = response.json().get("seq", "?")
        except Exception:
            seq = "?"
        logger(f"backup: uploaded v{seq}; local snapshot {snapshot.root_hash[:12]}")
        # Keep every distinct local snapshot. The store is content-addressed,
        # so unchanged files and unchanged snapshot roots consume no duplicate
        # object storage. Explicit operator-invoked GC remains available.
        return True

    logger(f"backup: upload failed HTTP {response.status_code}: {response.text[:200]}")
    return False
