"""locks -- extracted from backup.py."""
from __future__ import annotations

from core.backup._common import (
    BinaryIO,
    DATA_ROOT,
    Iterator,
    MANIFEST_ARCHIVE_PREFIX,
    Path,
    RELEASE_SEQUENCE_FLOOR_LOCK,
    SnapshotIntegrityError,
    _SNAPSHOT_LOCKS,
    _SNAPSHOT_LOCKS_GUARD,
    _SNAPSHOT_LOCK_LOCAL,
    chmod_open_file,
    contextmanager,
    data_policy,
    load_glass_config,
    lock_handle,
    os,
    stat,
    threading,
    time,
    unlock_handle,
)


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
def _snapshot_thread_lock(key: str) -> threading.RLock:
    with _SNAPSHOT_LOCKS_GUARD:
        return _SNAPSHOT_LOCKS.setdefault(key, threading.RLock())
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
        chmod_open_file(descriptor, path, 0o600)
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
            lock_handle(handle)
            depths[key] = 1
            yield
        finally:
            depths.pop(key, None)
            try:
                unlock_handle(handle)
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
            lock_handle(handle)
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
                unlock_handle(handle)
            finally:
                handle.close()
