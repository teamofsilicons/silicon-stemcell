"""store -- extracted from backup.py."""
from __future__ import annotations

from core.backup._common import (
    BinaryIO,
    MAINTENANCE_STATE,
    Path,
    SnapshotIntegrityError,
    SnapshotLimitError,
    SnapshotLimits,
    _DIGEST_RE,
    chmod_open_file,
    data_policy,
    fsync_directory,
    hashlib,
    nullcontext,
    os,
    stat,
    state_file_lock,
    tempfile,
)


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
def _source_only_appended(before: os.stat_result, after: os.stat_result) -> bool:
    """True when the copied prefix is still exactly what was on disk.

    A live silicon appends continuously to its interface inbox and run logs.
    Requiring a byte-identical stat before and after made those files
    unsnapshotable while the silicon was running: every attempt raced an append,
    the pre-update backup failed, and the whole update rolled back.

    Copying exactly ``before.st_size`` bytes makes an append harmless -- those
    bytes cannot change under us, only new ones are added past the end. So the
    snapshot is rejected only when the file was replaced (a different inode) or
    truncated (it shrank), where the copied prefix may correspond to no version
    that ever existed. An in-place rewrite that keeps the inode and does not
    shrink remains indistinguishable by stat alone, exactly as before.
    """

    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        return False
    return after.st_size >= before.st_size
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
            chmod_open_file(descriptor, temporary, 0o600)
            lock = (
                state_file_lock(root / protected.relative_path)
                if protected.relative_path == MAINTENANCE_STATE
                else nullcontext()
            )
            with lock:
                source, before = _safe_source_open(root, protected.relative_path)
                try:
                    if before.st_size > limits.max_file_size:
                        raise SnapshotLimitError(
                            f"{protected.relative_path} exceeds max_file_size."
                        )
                    digest = hashlib.sha256()
                    copied = 0
                    # Copy exactly the bytes that existed when we stat'd the
                    # source. Reading to EOF would chase a live append forever.
                    remaining = before.st_size
                    with os.fdopen(descriptor, "wb") as destination:
                        descriptor = -1
                        while remaining > 0:
                            chunk = source.read(min(limits.chunk_size, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
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
            if copied != before.st_size or not _source_only_appended(before, after):
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
                fsync_directory(object_path.parent)
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
