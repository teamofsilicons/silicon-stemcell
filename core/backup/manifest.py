"""manifest -- extracted from backup.py."""
from __future__ import annotations

from core.backup._common import (
    Iterable,
    LEGACY_DEFAULT_MANIFEST,
    MANIFEST_HEADER,
    MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    Mapping,
    Path,
    SNAPSHOT_SCHEMA,
    Sequence,
    SnapshotError,
    SnapshotIntegrityError,
    _DIGEST_RE,
    atomic_write_bytes,
    data_policy,
    hashlib,
    json,
    os,
    stat,
)
from core.backup.locks import (
    _unique_manifest_archive_path,
)
from core.backup.store import (
    _stat_signature,
)


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
        atomic_write_bytes(
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
        atomic_write_bytes(
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
