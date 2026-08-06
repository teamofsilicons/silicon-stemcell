"""gc -- extracted from backup.py."""
from __future__ import annotations

from core.backup._common import (
    DEFAULT_SNAPSHOT_RETENTION,
    Iterable,
    Mapping,
    Path,
    SnapshotError,
    SnapshotGCLimits,
    SnapshotGCPlan,
    SnapshotIntegrityError,
    SnapshotLimitError,
    SnapshotLimits,
    _DIGEST_RE,
    _MANIFEST_FILE_RE,
    _ManifestRecord,
    _OBJECT_PREFIX_RE,
    _OBJECT_SUFFIX_RE,
    data_policy,
    fsync_directory,
    os,
    stat,
)
from core.backup.locks import (
    _snapshot_store_lock,
)
from core.backup.manifest import (
    _load_manifest,
)
from core.backup.snapshot import (
    discover_protected_snapshot_roots,
)
from core.backup.store import (
    _verify_object,
)


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
        fsync_directory(directory)

    modified_directories.clear()
    for path in plan.delete_objects:
        path.unlink()
        modified_directories.add(path.parent)
    for directory in sorted(modified_directories):
        fsync_directory(directory)
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
