"""restore -- extracted from backup.py."""
from __future__ import annotations

from interface.backup._common import (
    IN_PLACE_RESTORE_JOURNAL,
    IN_PLACE_RESTORE_LATEST,
    MAX_RESTORE_JOURNAL_BYTES,
    Mapping,
    Path,
    RELEASE_SEQUENCE_FLOOR,
    RestorePlan,
    SnapshotError,
    SnapshotIntegrityError,
    SnapshotLimits,
    _DIGEST_RE,
    _ReleaseFloorPlan,
    atomic_write_bytes,
    data_policy,
    fsync_directory,
    json,
    os,
    shutil,
    stat,
    tempfile,
    time,
)
from interface.backup.fileops import (
    _restore_manifest_file,
    _restore_manifest_tombstone,
    _restore_parent,
    _restore_tombstone_destination,
    _validate_in_place_restore_path,
)
from interface.backup.gc import (
    _canonical_store_if_present,
)
from interface.backup.locks import (
    _release_floor_lock,
    _snapshot_store_lock,
)
from interface.backup.manifest import (
    _load_manifest,
)
from interface.backup.release_floor import (
    _plan_release_sequence_floor_restore,
    _restore_release_sequence_floor,
    _verify_release_sequence_floor,
)
from interface.backup.snapshot import (
    verify_local_snapshot,
)
from interface.backup.store import (
    _hash_file,
    _open_verified_object,
)


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
        fsync_directory(target.parent)
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
    atomic_write_bytes(
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
