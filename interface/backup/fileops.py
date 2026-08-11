"""fileops -- extracted from backup.py."""
from __future__ import annotations

from interface.backup._common import (
    Mapping,
    Path,
    PurePath,
    RELEASE_SEQUENCE_FLOOR,
    SnapshotIntegrityError,
    data_policy,
    fsync_directory,
    os,
    shutil,
    stat,
)
from interface.backup.store import (
    _hash_file,
    _open_verified_object,
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
                fsync_directory(parent)
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
        fsync_directory(parent)
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
    fsync_directory(destination.parent)
