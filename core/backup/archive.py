"""archive -- extracted from backup.py."""
from __future__ import annotations

from core.backup._common import (
    BinaryIO,
    CHUNK_SIZE,
    Callable,
    DEFAULT_SPOOL_LIMIT,
    Iterable,
    Mapping,
    Path,
    PurePath,
    Sequence,
    SnapshotIntegrityError,
    UPLOAD_PATH,
    UPLOAD_TIMEOUT,
    data_policy,
    gzip,
    json,
    os,
    load_glass_config,
    silicon_api_request,
    stat,
    tarfile,
    tempfile,
)
from core.backup.locks import (
    _instance_root,
)
from core.backup.manifest import (
    installed_release_id,
    load_policy,
)
from core.backup.snapshot import (
    create_local_snapshot,
)
from core.backup.store import (
    _open_verified_object,
    _safe_source_open,
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

    config, _config_path = load_glass_config(root)
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
