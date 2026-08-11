"""Verified, policy-driven snapshots and Glass backup compatibility.

The durable local format is a content-addressed object store plus a canonical
manifest.  Files are copied and hashed in chunks, so snapshot and restore
memory use is bounded independently of data size.  The legacy Glass endpoint
still receives a disk-spooled ``backup.tar.gz`` multipart file.

Implementation lives in the submodules (``store``, ``locks``, ``manifest``,
``fileops``, ``release_floor``, ``snapshot``, ``gc``, ``restore``,
``archive``); only the names below are part of the package surface.
"""
from __future__ import annotations

from core.backup._common import (  # noqa: F401
    DEFAULT_MANIFEST,
    IN_PLACE_RESTORE_JOURNAL,
    IN_PLACE_RESTORE_LATEST,
    LEGACY_DEFAULT_MANIFEST,
    MAINTENANCE_STATE,
    MANIFEST_HEADER,
    RELEASE_SEQUENCE_FLOOR,
    RELEASE_SEQUENCE_FLOOR_LOCK,
    SNAPSHOT_SCHEMA,
    SnapshotError,
    SnapshotGCLimits,
    SnapshotIntegrityError,
    SnapshotLimitError,
    SnapshotLimits,
    SnapshotResult,
    fcntl,
    json,
    state_file_lock,
    time,
)

from core.backup.store import (  # noqa: F401
    _source_only_appended,
)

from core.backup.locks import (  # noqa: F401
    _instance_root,
    _release_floor_lock,
    _snapshot_store_lock,
)

from core.backup.manifest import (  # noqa: F401
    ensure_manifest_file,
    installed_release_id,
    load_policy,
    read_manifest,
)

from core.backup.snapshot import (  # noqa: F401
    create_local_snapshot,
    discover_protected_snapshot_roots,
    verify_local_snapshot,
)

from core.backup.gc import (  # noqa: F401
    garbage_collect_referenced_snapshots,
    garbage_collect_snapshots,
    plan_snapshot_gc,
)

from core.backup.restore import (  # noqa: F401
    restore_local_snapshot_in_place,
    restore_snapshot,
)

from core.backup.archive import (  # noqa: F401
    build_archive,
    build_archive_file,
    run_backup,
)
