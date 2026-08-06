"""Verified, policy-driven snapshots and Glass backup compatibility.

The durable local format is a content-addressed object store plus a canonical
manifest.  Files are copied and hashed in chunks, so snapshot and restore
memory use is bounded independently of data size.  The legacy Glass endpoint
still receives a disk-spooled ``backup.tar.gz`` multipart file and the public
``run_backup``/``build_archive`` APIs remain compatible.

The implementation is split by concern; this module re-exports the whole
surface so ``core.backup.<name>`` keeps working:

    _common        constants, error types, and the snapshot dataclasses
    store          content-addressed object store: hashing, copy, verify
    locks          advisory snapshot-store and release-floor locks
    manifest       canonical manifest read/write/validate
    fileops        single-file restore primitives shared by restore paths
    release_floor  release-sequence floor read/plan/restore/verify
    snapshot       snapshot creation, verification, root discovery
    gc             snapshot garbage collection planning and execution
    restore        full restore, including in-place restore
    archive        tar/gzip archive building and Glass upload
"""
from __future__ import annotations

from core.backup._common import (  # noqa: F401
    BinaryIO,
    CHUNK_SIZE,
    Callable,
    DATA_ROOT,
    DEFAULT_MANIFEST,
    DEFAULT_SNAPSHOT_RETENTION,
    DEFAULT_SPOOL_LIMIT,
    IN_PLACE_RESTORE_JOURNAL,
    IN_PLACE_RESTORE_LATEST,
    Iterable,
    Iterator,
    LEGACY_DEFAULT_MANIFEST,
    MAINTENANCE_STATE,
    MANIFEST_ARCHIVE_PREFIX,
    MANIFEST_HEADER,
    MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    MAX_RELEASE_SEQUENCE_FLOOR_BYTES,
    MAX_RESTORE_JOURNAL_BYTES,
    MAX_UPDATE_JOURNAL_BYTES,
    Mapping,
    Path,
    PurePath,
    RELEASE_SEQUENCE_FLOOR,
    RELEASE_SEQUENCE_FLOOR_LOCK,
    RestorePlan,
    SNAPSHOT_SCHEMA,
    Sequence,
    SnapshotError,
    SnapshotGCLimits,
    SnapshotGCPlan,
    SnapshotIntegrityError,
    SnapshotLimitError,
    SnapshotLimits,
    SnapshotResult,
    UPLOAD_PATH,
    UPLOAD_TIMEOUT,
    _DIGEST_RE,
    _GIT_RELEASE_VERSION_RE,
    _MANIFEST_FILE_RE,
    _ManifestRecord,
    _OBJECT_PREFIX_RE,
    _OBJECT_SUFFIX_RE,
    _RELEASE_FLOOR_TRUST,
    _ReleaseFloorPlan,
    _SNAPSHOT_LOCKS,
    _SNAPSHOT_LOCKS_GUARD,
    _SNAPSHOT_LOCK_LOCAL,
    _UPDATE_STATES,
    _UPDATE_TRANSACTION_ID_RE,
    atomic_write_bytes,
    chmod_open_file,
    contextmanager,
    data_policy,
    dataclass,
    fcntl,
    fsync_directory,
    gzip,
    hashlib,
    json,
    load_glass_config,
    lock_handle,
    math,
    msvcrt,
    nullcontext,
    os,
    re,
    shutil,
    silicon_api_request,
    stat,
    state_file_lock,
    tarfile,
    tempfile,
    threading,
    time,
    unlock_handle,
)
from core.backup.store import (  # noqa: F401
    _copy_source_to_object,
    _hash_file,
    _object_path,
    _open_verified_object,
    _prepare_store,
    _safe_source_open,
    _source_only_appended,
    _stat_signature,
    _verify_object,
)
from core.backup.locks import (  # noqa: F401
    _instance_root,
    _load_glass_config,
    _release_floor_lock,
    _secure_lock_file,
    _snapshot_store_lock,
    _snapshot_thread_lock,
    _unique_manifest_archive_path,
)
from core.backup.manifest import (  # noqa: F401
    _canonical_json,
    _load_manifest,
    _manifest_body,
    _manifest_path,
    _normalise_release_id,
    _read_bounded_json_object,
    _read_manifest_json,
    _validate_manifest,
    ensure_manifest_file,
    installed_release_id,
    load_policy,
    read_manifest,
)
from core.backup.fileops import (  # noqa: F401
    _restore_manifest_file,
    _restore_manifest_tombstone,
    _restore_parent,
    _restore_tombstone_destination,
    _validate_in_place_restore_path,
)
from core.backup.release_floor import (  # noqa: F401
    _plan_release_sequence_floor_restore,
    _read_local_release_sequence_floor,
    _read_snapshot_release_sequence_floor,
    _restore_release_sequence_floor,
    _validate_release_sequence_floor,
    _verify_release_sequence_floor,
)
from core.backup.snapshot import (  # noqa: F401
    _add_checkpoint_root,
    _create_local_snapshot_locked,
    _snapshot_roots_from_update_journal,
    create_local_snapshot,
    discover_protected_snapshot_roots,
    verify_local_snapshot,
)
from core.backup.gc import (  # noqa: F401
    _apply_snapshot_gc_plan,
    _assert_canonical_delete_path,
    _canonical_store_if_present,
    _empty_gc_plan,
    _normalise_gc_parameters,
    _plan_snapshot_gc_locked,
    _require_store_subdirectory,
    _scan_manifest_records,
    _scan_object_records,
    _verify_retained_manifests,
    garbage_collect_referenced_snapshots,
    garbage_collect_snapshots,
    plan_snapshot_gc,
)
from core.backup.restore import (  # noqa: F401
    _read_restore_journal,
    _restore_local_snapshot_in_place_locked,
    _verify_in_place_restore,
    _write_restore_journal,
    plan_restore,
    restore_local_snapshot_in_place,
    restore_snapshot,
)
from core.backup.archive import (  # noqa: F401
    _archive_snapshot_file,
    _tar_directory_info,
    _tar_file_info,
    _write_archive,
    build_archive,
    build_archive_file,
    run_backup,
)
