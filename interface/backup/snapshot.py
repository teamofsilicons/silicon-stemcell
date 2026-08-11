"""snapshot -- extracted from backup.py."""
from __future__ import annotations

from interface.backup._common import (
    IN_PLACE_RESTORE_JOURNAL,
    IN_PLACE_RESTORE_LATEST,
    MAX_RESTORE_JOURNAL_BYTES,
    MAX_UPDATE_JOURNAL_BYTES,
    Mapping,
    Path,
    RELEASE_SEQUENCE_FLOOR,
    SnapshotIntegrityError,
    SnapshotLimitError,
    SnapshotLimits,
    SnapshotResult,
    _DIGEST_RE,
    _UPDATE_STATES,
    _UPDATE_TRANSACTION_ID_RE,
    atomic_write_bytes,
    data_policy,
    hashlib,
    os,
    stat,
)
from interface.backup.locks import (
    _snapshot_store_lock,
)
from interface.backup.manifest import (
    _canonical_json,
    _load_manifest,
    _manifest_body,
    _manifest_path,
    _read_bounded_json_object,
    _validate_manifest,
    load_policy,
)
from interface.backup.release_floor import (
    _read_snapshot_release_sequence_floor,
)
from interface.backup.store import (
    _copy_source_to_object,
    _prepare_store,
    _verify_object,
)


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
        atomic_write_bytes(manifest_path, manifest_bytes, mode=0o400)
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
