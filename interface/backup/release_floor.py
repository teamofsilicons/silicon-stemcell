"""release_floor -- extracted from backup.py."""
from __future__ import annotations

from interface.backup._common import (
    MAX_RELEASE_SEQUENCE_FLOOR_BYTES,
    Mapping,
    Path,
    RELEASE_SEQUENCE_FLOOR,
    SnapshotIntegrityError,
    _DIGEST_RE,
    _GIT_RELEASE_VERSION_RE,
    _RELEASE_FLOOR_TRUST,
    _ReleaseFloorPlan,
    json,
    math,
)
from interface.backup.fileops import (
    _restore_manifest_file,
)
from interface.backup.manifest import (
    _read_bounded_json_object,
)
from interface.backup.store import (
    _open_verified_object,
)


def _validate_release_sequence_floor(
    value: Mapping[str, object],
    *,
    label: str,
) -> tuple[int, str]:
    """Return the authenticated release identity encoded by a floor record."""

    sequence = value.get("sequence")
    tree_sha256 = value.get("tree_sha256")
    recorded_at = value.get("recorded_at")
    schema = value.get("schema")
    expected = (
        {"schema", "sequence", "tree_sha256", "recorded_at"}
        if schema == 1
        else {
            "schema",
            "sequence",
            "version",
            "trust",
            "tree_sha256",
            "recorded_at",
        }
    )
    if (
        schema not in {1, 2}
        or set(value) != expected
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
    if schema == 2:
        version = value.get("version")
        trust = value.get("trust")
        if (
            not isinstance(version, str)
            or not version
            or len(version) > 64
            or "\x00" in version
            or trust not in _RELEASE_FLOOR_TRUST
        ):
            raise SnapshotIntegrityError(f"{label} is invalid.")
        if trust == "git-semver-tag":
            match = _GIT_RELEASE_VERSION_RE.fullmatch(version)
            if match is None:
                raise SnapshotIntegrityError(f"{label} is invalid.")
            major, minor, patch = (int(part) for part in match.groups())
            expected_sequence = major * 1_000_000 + minor * 1_000 + patch + 1
            if sequence != expected_sequence:
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
