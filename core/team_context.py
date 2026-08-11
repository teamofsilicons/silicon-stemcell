"""Mirror Glass-owned Silicon team context into the local prompt tree.

Glass is authoritative for ``prompts/TEAM.md`` and peer advertising memories.
The authenticated Silicon authors only its own
``prompts/advertising/<silicon_id>.md``.  This module keeps a content-free sync
state so conditional requests, optimistic concurrency, and local drafts survive
process restarts.

Public entrypoints are deliberately fail-open: network, authentication, and
filesystem failures are returned as non-sensitive result dictionaries rather
than escaping into the manager or Glass-agent loops.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import stat
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from core.glass import (
    CONFIG_FILE,
    authenticated_server_url,
    load_glass_config,
    silicon_api_request,
)
from core.runtime_paths import DATA_ROOT
from core.state_store import (
    atomic_write_bytes,
    fsync_directory,
    lock_handle,
    unlock_handle,
)

PROJECT_ROOT = DATA_ROOT
TEAM_CONTEXT_PATH = "prompts/TEAM.md"
ADVERTISING_DIRECTORY = "prompts/advertising"
STATE_PATH = "core/interface_state/team_context.json"
LOCK_PATH = "core/interface_state/team_context.lock"
DRAFT_ARCHIVE_DIRECTORY = "core/interface_state/team_context_drafts"
VISIBILITY_BLOCK_PATH = "core/interface_state/team_context.blocked"

MAX_ADVERTISING_MEMORY_LINES = 100
MAX_ADVERTISING_MEMORY_BYTES = 64 * 1024
MAX_ADVERTISED_MEMORY_LINES = 600
MAX_ADVERTISED_MEMORY_BYTES = 256 * 1024
MAX_TEAM_CONTEXT_BYTES = 256 * 1024
RECONCILE_INTERVAL_SECONDS = 60
LOCK_TIMEOUT_SECONDS = 10
MAX_PARALLEL_PEER_SYNCS = 4

TEAM_PLACEHOLDER_MARKDOWN = """# Silicon Team

_Team context has not been fetched from Glass yet._

Glass replaces this placeholder with the current team hierarchy and each
Silicon's name, description, job description, and advertising-memory path.
Advertising-memory contents are never embedded in this file.
"""

_STATE_VERSION = 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SILICON_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_TEAM_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$")
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
log = logging.getLogger(__name__)


class TeamContextError(RuntimeError):
    """A finite synchronization or remote-contract failure."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TeamContextLockTimeout(TeamContextError):
    pass


class TeamContextIdentityChanged(TeamContextError):
    pass


def _validate_memory(content: str, max_lines: int, max_bytes: int, oversize: str) -> str:
    if not isinstance(content, str):
        raise ValueError("Advertising memory content must be a string.")
    if "\x00" in content:
        raise ValueError("Advertising memory cannot contain NUL characters.")
    if len(content.splitlines()) > max_lines:
        raise ValueError(oversize.format(limit=max_lines, unit="lines"))
    if len(content.encode("utf-8")) > max_bytes:
        raise ValueError(oversize.format(limit=max_bytes, unit="UTF-8 bytes"))
    return content


def validate_advertising_memory(content: str) -> str:
    """Validate the exact Glass advertising-memory limits without truncating."""
    return _validate_memory(
        content,
        MAX_ADVERTISING_MEMORY_LINES,
        MAX_ADVERTISING_MEMORY_BYTES,
        "Advertising memory cannot exceed {limit} {unit}.",
    )


def validate_advertised_memory(content: str) -> str:
    """Validate a Glass-composed peer memory with its managed integration block."""
    return _validate_memory(
        content,
        MAX_ADVERTISED_MEMORY_LINES,
        MAX_ADVERTISED_MEMORY_BYTES,
        "Advertised memory cannot exceed {limit} {unit}.",
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _default_state() -> dict[str, Any]:
    return {
        "version": _STATE_VERSION,
        "identity": {},
        "context": {},
        "peers": {},
        "managed_peer_ids": [],
        "own": {},
        "draft_archives": [],
        "schedule": {},
    }


def _normalise_root(root: str | Path | None) -> Path:
    return Path(root or PROJECT_ROOT).resolve()


def _state_file(root: Path) -> Path:
    return root / STATE_PATH


def _lock_file(root: Path) -> Path:
    return root / LOCK_PATH


def _team_file(root: Path) -> Path:
    return root / TEAM_CONTEXT_PATH


def _visibility_block_file(root: Path) -> Path:
    return root / VISIBILITY_BLOCK_PATH


def _advertising_file(root: Path, silicon_id: str) -> Path:
    _validate_identifier(silicon_id, "Silicon ID")
    return root / ADVERTISING_DIRECTORY / f"{silicon_id}.md"


def _validate_identifier(value: Any, label: str) -> str:
    value = str(value or "").strip()
    pattern = _TEAM_SLUG_RE if label == "team slug" else _SILICON_ID_RE
    if not pattern.fullmatch(value):
        raise TeamContextError(f"Glass returned an invalid {label}.")
    return value


def _assert_local_path(root: Path, path: Path) -> None:
    root_resolved = root.resolve()
    parent_resolved = path.parent.resolve(strict=False)
    try:
        common = Path(os.path.commonpath((str(root_resolved), str(parent_resolved))))
    except ValueError as exc:
        raise TeamContextError(
            "Generated context path escapes the Silicon root."
        ) from exc
    if common != root_resolved:
        raise TeamContextError("Generated context path escapes the Silicon root.")


def _atomic_write_bytes(root: Path, path: Path, data: bytes) -> None:
    """Write inside the Silicon root only, then persist atomically."""
    _assert_local_path(root, path)
    atomic_write_bytes(path, data, dir_mode=None)


def _write_team_placeholder(root: Path) -> None:
    _atomic_write_bytes(
        root,
        _team_file(root),
        TEAM_PLACEHOLDER_MARKDOWN.encode("utf-8"),
    )


def ensure_team_context_layout(
    root: str | Path | None = None,
) -> dict[str, str]:
    """Ensure the pre-fetch TEAM placeholder and advertising directory exist."""

    project_root = _normalise_root(root)
    advertising_directory = project_root / ADVERTISING_DIRECTORY
    _assert_local_path(project_root, advertising_directory)
    advertising_directory.mkdir(parents=True, exist_ok=True)
    directory_mode = os.lstat(advertising_directory).st_mode
    if stat.S_ISLNK(directory_mode) or not stat.S_ISDIR(directory_mode):
        raise TeamContextError(
            "Advertising-memory path must be a local directory."
        )
    _assert_local_path(
        project_root,
        advertising_directory / ".layout-containment-check",
    )

    team_path = _team_file(project_root)
    if os.path.lexists(team_path):
        team_mode = os.lstat(team_path).st_mode
        if stat.S_ISLNK(team_mode) or not stat.S_ISREG(team_mode):
            raise TeamContextError("TEAM.md must be a local regular file.")
    else:
        _write_team_placeholder(project_root)

    return {
        "team_path": TEAM_CONTEXT_PATH,
        "advertising_directory": ADVERTISING_DIRECTORY,
    }


def _ensure_private_archive_directory(root: Path, path: Path) -> None:
    """Create one contained archive directory without accepting a symlink."""

    _assert_local_path(root, path)
    path.mkdir(parents=True, exist_ok=True)
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise TeamContextError(
            "Advertising-memory draft archive must be a local directory."
        )
    # ``_assert_local_path`` validates a path's resolved parent.  Validate a
    # hypothetical child so this directory itself is included in containment
    # checking after it has been created.
    _assert_local_path(root, path / ".archive-containment-check")
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except (NotImplementedError, OSError):
        pass
    after = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(after.st_mode) or not os.path.samestat(before, after):
        raise TeamContextError(
            "Advertising-memory draft archive changed during validation."
        )
    _assert_local_path(root, path / ".archive-containment-check")


def _load_state(root: Path) -> dict[str, Any]:
    path = _state_file(root)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return _default_state()
    if not isinstance(body, dict) or body.get("version") != _STATE_VERSION:
        return _default_state()
    state = _default_state()
    state.update(body)
    for key, fallback in (
        ("identity", {}),
        ("context", {}),
        ("peers", {}),
        ("own", {}),
        ("schedule", {}),
    ):
        if not isinstance(state.get(key), dict):
            state[key] = fallback
    if not isinstance(state.get("managed_peer_ids"), list):
        state["managed_peer_ids"] = []
    if not isinstance(state.get("draft_archives"), list):
        state["draft_archives"] = []
    schedule = state["schedule"]
    now = time.time()
    for key in ("last_reconcile_at", "last_attempt_at"):
        schedule[key] = _safe_schedule_timestamp(
            schedule.get(key),
            now=now,
            allow_future=False,
        )
    schedule["next_reconcile_at"] = _safe_schedule_timestamp(
        schedule.get("next_reconcile_at"),
        now=now,
        allow_future=True,
    )
    failure_count = schedule.get("failure_count")
    if (
        isinstance(failure_count, bool)
        or not isinstance(failure_count, int)
        or failure_count < 0
    ):
        failure_count = 0
    schedule["failure_count"] = min(failure_count, 100)
    return state


def _safe_schedule_timestamp(
    value: Any,
    *,
    now: float,
    allow_future: bool,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(parsed) or parsed < 0:
        return 0.0
    if not allow_future and parsed > now + RECONCILE_INTERVAL_SECONDS:
        return 0.0
    if allow_future and parsed > now + (2 * RECONCILE_INTERVAL_SECONDS):
        return 0.0
    return parsed


def _save_state(root: Path, state: dict[str, Any]) -> None:
    state["version"] = _STATE_VERSION
    encoded = (
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    path = _state_file(root)
    try:
        if path.read_bytes() == encoded:
            return
    except OSError:
        pass
    _atomic_write_bytes(root, path, encoded)


def own_advertising_signature(
    root: str | Path | None = None,
) -> tuple[int, int, int, int] | None:
    """Return a content-change signature without performing reconciliation."""
    project_root = _normalise_root(root)
    try:
        state = _load_state(project_root)
        identity = _stored_identity(state)
        if identity is None:
            return None
        metadata = os.stat(
            _advertising_file(project_root, identity["silicon_id"]),
            follow_symlinks=False,
        )
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )
    except (OSError, TeamContextError):
        return None


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _sync_lock(root: Path) -> Iterator[None]:
    path = _lock_file(root)
    _assert_local_path(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    before: os.stat_result | None = None
    if os.path.lexists(path):
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise TeamContextError("Team context lock must be a local regular file.")
    local_lock = _thread_lock(path)
    if not local_lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
        raise TeamContextLockTimeout("Team context synchronization is already running.")

    handle = None
    acquired = False
    try:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        handle = os.fdopen(os.open(path, flags, 0o600), "r+b")
        opened = os.fstat(handle.fileno())
        after = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not os.path.samestat(opened, after)
            or (before is not None and not os.path.samestat(before, opened))
        ):
            raise TeamContextError(
                "Team context lock changed while it was being opened."
            )
        _assert_local_path(root, path)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while not lock_handle(handle, blocking=False):
            if time.monotonic() >= deadline:
                raise TeamContextLockTimeout(
                    "Team context synchronization is already running."
                )
            time.sleep(0.05)
        acquired = True
        yield
    finally:
        if handle is not None:
            if acquired:
                try:
                    unlock_handle(handle)
                except OSError:
                    pass
            handle.close()
        local_lock.release()


def _response_json(response: Any, operation: str) -> dict[str, Any]:
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise TeamContextError(f"Glass returned invalid JSON for {operation}.") from exc
    if not isinstance(body, dict):
        raise TeamContextError(f"Glass returned an invalid object for {operation}.")
    return body


def _expect_status(response: Any, expected: set[int], operation: str) -> int:
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in expected:
        raise TeamContextError(
            f"Glass {operation} failed with HTTP {status or 'unknown'}.",
            status_code=status or None,
        )
    return status


def _etag(response: Any) -> str:
    headers = getattr(response, "headers", {}) or {}
    value = str(headers.get("ETag") or headers.get("etag") or "")
    if len(value) > 512 or "\r" in value or "\n" in value:
        return ""
    return value


def _request(
    root: Path,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    timeout: int | float = 10,
) -> Any:
    return silicon_api_request(
        method,
        path,
        config=config,
        start=root,
        headers=headers,
        json_body=body,
        timeout=timeout,
    )


def _validated_server_origin(value: Any) -> str:
    origin = str(value or "").strip()
    if not origin or len(origin) > 2048:
        raise TeamContextError("Glass returned an invalid server origin.")
    try:
        validated = authenticated_server_url({"server_url": origin})
    except Exception as exc:
        raise TeamContextError("Glass returned an invalid server origin.") from exc
    if validated != origin.rstrip("/"):
        raise TeamContextError("Glass returned an invalid server origin.")
    return validated


def _credential_fingerprint(config: dict[str, Any]) -> str:
    key = str(
        config.get("api_key") or config.get("silicon_api_key") or ""
    ).strip()
    if not key:
        raise TeamContextError("Glass configuration does not contain a Silicon key.")
    return _sha256(b"team-context-credential\0" + key.encode("utf-8"))


def _validated_credential_fingerprint(
    value: Any,
    *,
    allow_empty: bool = False,
) -> str:
    fingerprint = str(value or "").lower()
    if allow_empty and not fingerprint:
        return ""
    if not _SHA256_RE.fullmatch(fingerprint):
        raise TeamContextError("Glass identity has an invalid credential scope.")
    return fingerprint


def _load_config_snapshot(root: Path) -> tuple[dict[str, Any], str]:
    config, config_path = load_glass_config(root)
    if (
        config_path.name != CONFIG_FILE
        or config_path.parent.resolve() != root.resolve()
    ):
        raise TeamContextError(
            "This Silicon does not have its own .glass.json configuration."
        )
    if not isinstance(config, dict):
        raise TeamContextError("Glass configuration must be a JSON object.")
    try:
        origin = authenticated_server_url(config)
    except Exception as exc:
        raise TeamContextError(
            "Glass configuration has an unsafe server origin."
        ) from exc
    return config, _validated_server_origin(origin)


def _fetch_identity(
    root: Path,
    *,
    config: dict[str, Any] | None = None,
    server_origin: str = "",
) -> dict[str, Any]:
    if config is None:
        config, configured_origin = _load_config_snapshot(root)
        server_origin = server_origin or configured_origin
    else:
        server_origin = server_origin or authenticated_server_url(config)
    response = _request(
        root,
        "GET",
        "/api/v1/silicons/me",
        config=config,
        timeout=8,
    )
    _expect_status(response, {200}, "identity request")
    body = _response_json(response, "identity request")
    if body.get("is_active") is False:
        raise TeamContextError(
            "Glass reports that this Silicon is inactive.", status_code=403
        )
    silicon_id = _validate_identifier(body.get("silicon_id"), "Silicon ID")
    team_slug = _validate_identifier(
        body.get("owner_team_slug") or body.get("team"),
        "team slug",
    )
    return {
        "silicon_id": silicon_id,
        "team_slug": team_slug,
        "server_origin": _validated_server_origin(server_origin),
        "credential_fingerprint": _credential_fingerprint(config),
        "access_valid": True,
    }


def _stored_identity(state: dict[str, Any]) -> dict[str, Any] | None:
    identity = state.get("identity")
    if not isinstance(identity, dict):
        return None
    try:
        return {
            "silicon_id": _validate_identifier(
                identity.get("silicon_id"), "Silicon ID"
            ),
            "team_slug": _validate_identifier(identity.get("team_slug"), "team slug"),
            "server_origin": _validated_server_origin(identity.get("server_origin")),
            "credential_fingerprint": _validated_credential_fingerprint(
                identity.get("credential_fingerprint"),
                allow_empty=True,
            ),
            "access_valid": identity.get("access_valid") is not False,
        }
    except TeamContextError:
        return None


def _cached_identity(state: dict[str, Any]) -> dict[str, Any] | None:
    identity = _stored_identity(state)
    if identity is None or not identity["access_valid"]:
        return None
    return identity


def _set_identity(state: dict[str, Any], identity: dict[str, Any]) -> bool:
    previous = _stored_identity(state)
    canonical = {
        "silicon_id": _validate_identifier(identity.get("silicon_id"), "Silicon ID"),
        "team_slug": _validate_identifier(identity.get("team_slug"), "team slug"),
        "server_origin": _validated_server_origin(identity.get("server_origin")),
        "credential_fingerprint": _validated_credential_fingerprint(
            identity.get("credential_fingerprint")
        ),
        "access_valid": True,
    }
    changed = previous != canonical
    if changed:
        principal_changed = previous is None or (
            previous["silicon_id"] != canonical["silicon_id"]
            or previous["server_origin"] != canonical["server_origin"]
        )
        state["identity"] = canonical
        state["context"] = {}
        if principal_changed:
            state["own"] = {}
    return changed


def _manifest_entry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TeamContextError("Glass returned an invalid advertising manifest entry.")
    silicon_id = _validate_identifier(raw.get("silicon_id"), "Silicon ID")
    expected_path = f"{ADVERTISING_DIRECTORY}/{silicon_id}.md"
    if raw.get("path") != expected_path:
        raise TeamContextError("Glass returned an invalid advertising-memory path.")
    revision = raw.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise TeamContextError("Glass returned an invalid advertising-memory revision.")
    digest = str(raw.get("sha256") or "").lower()
    if not _SHA256_RE.fullmatch(digest):
        raise TeamContextError("Glass returned an invalid advertising-memory hash.")
    updated_at = raw.get("updated_at")
    if updated_at is not None and (
        not isinstance(updated_at, str) or len(updated_at) > 100
    ):
        raise TeamContextError(
            "Glass returned an invalid advertising-memory timestamp."
        )
    return {
        "silicon_id": silicon_id,
        "path": expected_path,
        "revision": revision,
        "sha256": digest,
        "updated_at": updated_at,
    }


def _normalise_manifest(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        raise TeamContextError("Glass returned an invalid advertising-memory manifest.")
    manifest: dict[str, dict[str, Any]] = {}
    for item in raw:
        entry = _manifest_entry(item)
        silicon_id = entry["silicon_id"]
        if silicon_id in manifest:
            raise TeamContextError(
                "Glass returned duplicate advertising-memory entries."
            )
        manifest[silicon_id] = entry
    return manifest


def _validate_context_payload(
    payload: dict[str, Any],
    identity: dict[str, str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if payload.get("path") != TEAM_CONTEXT_PATH:
        raise TeamContextError("Glass returned an invalid TEAM.md path.")
    if str(payload.get("team_slug") or "") != identity["team_slug"]:
        raise TeamContextError("Glass returned context for the wrong team.")
    markdown = payload.get("markdown")
    if not isinstance(markdown, str):
        raise TeamContextError("Glass returned invalid TEAM.md content.")
    markdown_bytes = markdown.encode("utf-8")
    if len(markdown_bytes) > MAX_TEAM_CONTEXT_BYTES:
        raise TeamContextError(
            f"Glass TEAM.md exceeds {MAX_TEAM_CONTEXT_BYTES} UTF-8 bytes."
        )
    revision = str(payload.get("revision") or "").lower()
    if not _SHA256_RE.fullmatch(revision) or _sha256(markdown_bytes) != revision:
        raise TeamContextError("Glass returned a TEAM.md hash mismatch.")
    sync_revision = str(payload.get("sync_revision") or "").lower()
    if not _SHA256_RE.fullmatch(sync_revision):
        raise TeamContextError("Glass returned an invalid team sync revision.")
    manifest = _normalise_manifest(payload.get("advertising_memories"))
    if identity["silicon_id"] not in manifest:
        raise TeamContextError("Glass team context does not contain this Silicon.")
    team_id = str(payload.get("team_id") or "")
    if len(team_id) > 255:
        raise TeamContextError("Glass returned an invalid team ID.")
    context = {
        "team_id": team_id,
        "team_slug": identity["team_slug"],
        "path": TEAM_CONTEXT_PATH,
        "revision": revision,
        "sync_revision": sync_revision,
        "markdown": markdown,
    }
    return context, manifest


def _manifest_for_state(manifest: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [manifest[silicon_id] for silicon_id in sorted(manifest)]


def _manifest_from_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    context = state.get("context") or {}
    return _normalise_manifest(context.get("advertising_memories"))


def _context_matches_identity_scope(
    state: dict[str, Any],
    identity: dict[str, Any],
) -> bool:
    context = state.get("context")
    return bool(
        isinstance(context, dict)
        and context.get("team_slug") == identity["team_slug"]
        and context.get("server_origin") == identity["server_origin"]
        and context.get("credential_fingerprint")
        == identity["credential_fingerprint"]
    )


def _team_file_matches(root: Path, revision: str) -> bool:
    try:
        return (
            _sha256(
                _read_regular_bytes(
                    root,
                    _team_file(root),
                    max_bytes=MAX_TEAM_CONTEXT_BYTES,
                )
            )
            == revision
        )
    except (OSError, ValueError):
        return False


def _write_visibility_block(root: Path) -> None:
    """Atomically hide TEAM.md before a destructive mirror transition."""

    path = _visibility_block_file(root)
    if os.path.lexists(path):
        return
    _atomic_write_bytes(root, path, b"")


def _clear_visibility_block_if_verified(
    root: Path,
    state: dict[str, Any],
) -> None:
    identity = _cached_identity(state)
    context = state.get("context") or {}
    revision = str(context.get("revision") or "")
    if (
        identity is not None
        and _context_matches_identity_scope(state, identity)
        and bool(_SHA256_RE.fullmatch(revision))
        and _team_file_matches(root, revision)
    ):
        try:
            _visibility_block_file(root).unlink(missing_ok=True)
        except OSError:
            pass


def _get_context_response(
    root: Path,
    identity: dict[str, Any],
    state: dict[str, Any],
    *,
    force: bool,
    config: dict[str, Any] | None = None,
) -> Any:
    headers: dict[str, str] = {}
    etag = str((state.get("context") or {}).get("etag") or "")
    if etag and not force and _context_matches_identity_scope(state, identity):
        headers["If-None-Match"] = etag
    slug = quote(identity["team_slug"], safe="")
    return _request(
        root,
        "GET",
        f"/api/v1/teams/{slug}/silicon-context",
        headers=headers,
        config=config,
        timeout=12,
    )


def _read_regular_bytes(
    root: Path,
    path: Path,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read one unchanged regular file without following a symbolic link."""

    _assert_local_path(root, path)
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Local context path must be a regular file.")
    if max_bytes is not None and before.st_size > max_bytes:
        raise ValueError("Local context file exceeds its size limit.")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise ValueError("Local context file changed while it was being opened.")
        if max_bytes is not None and opened.st_size > max_bytes:
            raise ValueError("Local context file exceeds its size limit.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError("Local context file exceeds its size limit.")
            chunks.append(chunk)
        after = os.stat(path, follow_symlinks=False)
        if not os.path.samestat(opened, after):
            raise ValueError("Local context file changed while it was being read.")
        # Re-resolve the parent after reading so an ancestor symlink swap cannot
        # turn an external file into uploadable local advertising content.
        _assert_local_path(root, path)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _read_local_memory(
    root: Path,
    path: Path,
    *,
    allow_managed: bool = False,
) -> tuple[str, str]:
    try:
        raw = _read_regular_bytes(
            root,
            path,
            max_bytes=(
                MAX_ADVERTISED_MEMORY_BYTES
                if allow_managed
                else MAX_ADVERTISING_MEMORY_BYTES
            ),
        )
    except (ValueError, TeamContextError) as exc:
        if isinstance(exc, TeamContextError):
            raise ValueError(
                "Advertising memory path must remain inside the Silicon root."
            ) from exc
        if "size limit" in str(exc):
            maximum = (
                MAX_ADVERTISED_MEMORY_BYTES
                if allow_managed
                else MAX_ADVERTISING_MEMORY_BYTES
            )
            raise ValueError(
                f"Advertising memory cannot exceed {maximum} UTF-8 bytes."
            ) from exc
        raise
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Advertising memory must be valid UTF-8.") from exc
    (
        validate_advertised_memory(content)
        if allow_managed
        else validate_advertising_memory(content)
    )
    return content, _sha256(raw)


def _validate_memory_payload(
    payload: dict[str, Any],
    silicon_id: str,
    *,
    expected: dict[str, Any] | None = None,
    allow_managed: bool = False,
) -> dict[str, Any]:
    if str(payload.get("silicon_id") or "") != silicon_id:
        raise TeamContextError(
            "Glass returned advertising memory for the wrong Silicon."
        )
    expected_path = f"{ADVERTISING_DIRECTORY}/{silicon_id}.md"
    if payload.get("path") != expected_path:
        raise TeamContextError("Glass returned an invalid advertising-memory path.")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise TeamContextError("Glass returned an invalid advertising-memory revision.")
    content = payload.get("content")
    try:
        (
            validate_advertised_memory(content)
            if allow_managed
            else validate_advertising_memory(content)
        )
    except ValueError as exc:
        raise TeamContextError(str(exc)) from exc
    digest = str(payload.get("sha256") or "").lower()
    actual_digest = _sha256(content.encode("utf-8"))
    if not _SHA256_RE.fullmatch(digest) or digest != actual_digest:
        raise TeamContextError("Glass returned an advertising-memory hash mismatch.")
    if expected and (expected["revision"] != revision or expected["sha256"] != digest):
        raise TeamContextError("Glass advertising memory does not match its manifest.")
    updated_at = payload.get("updated_at")
    if updated_at is not None and (
        not isinstance(updated_at, str) or len(updated_at) > 100
    ):
        raise TeamContextError(
            "Glass returned an invalid advertising-memory timestamp."
        )
    return {
        "silicon_id": silicon_id,
        "path": expected_path,
        "revision": revision,
        "sha256": digest,
        "updated_at": updated_at,
        "content": content,
    }


def _fetch_peer(
    root: Path,
    identity: dict[str, Any],
    entry: dict[str, Any],
    *,
    etag: str = "",
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    headers = {"If-None-Match": etag} if etag else {}
    slug = quote(identity["team_slug"], safe="")
    silicon_id = quote(entry["silicon_id"], safe="")
    response = _request(
        root,
        "GET",
        f"/api/v1/teams/{slug}/advertising-memories/{silicon_id}",
        headers=headers,
        config=config,
        timeout=10,
    )
    status = _expect_status(response, {200, 304}, "peer advertising-memory request")
    if status == 304:
        return None, _etag(response) or etag
    payload = _response_json(response, "peer advertising-memory request")
    return (
        _validate_memory_payload(
            payload,
            entry["silicon_id"],
            expected=entry,
            allow_managed=True,
        ),
        _etag(response),
    )


def _sync_peer(
    root: Path,
    identity: dict[str, Any],
    entry: dict[str, Any],
    old_record: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool, bytes | None]:
    """Fetch and validate one peer, leaving publication to the lock owner."""

    path = _advertising_file(root, entry["silicon_id"])
    try:
        _content, local_digest = _read_local_memory(
            root,
            path,
            allow_managed=True,
        )
    except (OSError, ValueError):
        local_digest = ""
    if local_digest == entry["sha256"]:
        return {
            **entry,
            "etag": str(old_record.get("etag") or ""),
        }, False, None

    remote, response_etag = _fetch_peer(
        root,
        identity,
        entry,
        etag=str(old_record.get("etag") or ""),
        config=config,
    )
    if remote is None:
        # A cached ETag cannot repair a missing or locally modified mirror.
        remote, response_etag = _fetch_peer(
            root,
            identity,
            entry,
            config=config,
        )
    if remote is None:
        raise TeamContextError("Glass did not return the required peer memory.")
    return {
        **entry,
        "etag": response_etag,
    }, True, remote["content"].encode("utf-8")


def _peer_file_matches_record(
    root: Path,
    silicon_id: str,
    record: dict[str, Any],
) -> bool:
    """Return whether a peer mirror still matches a validated Glass record."""

    try:
        validated = _manifest_entry(record)
        if validated["silicon_id"] != silicon_id:
            return False
        _content, local_digest = _read_local_memory(
            root,
            _advertising_file(root, silicon_id),
            allow_managed=True,
        )
        return local_digest == validated["sha256"]
    except (OSError, ValueError, TeamContextError):
        return False


def _remove_unverified_peer_file(root: Path, silicon_id: str) -> bool:
    path = _advertising_file(root, silicon_id)
    _assert_local_path(root, path)
    existed = os.path.lexists(path)
    if existed:
        _write_visibility_block(root)
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)
    return existed


def _fetch_own_remote(
    root: Path,
    identity: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    response = _request(
        root,
        "GET",
        "/api/v1/silicons/me/advertising-memory",
        config=config,
        timeout=10,
    )
    _expect_status(response, {200}, "own advertising-memory request")
    payload = _response_json(response, "own advertising-memory request")
    return _validate_memory_payload(payload, identity["silicon_id"]), _etag(response)


def _set_own_base(
    state: dict[str, Any],
    identity: dict[str, Any],
    remote: dict[str, Any],
    *,
    etag: str = "",
) -> None:
    state["own"] = {
        "silicon_id": identity["silicon_id"],
        "base_revision": remote["revision"],
        "base_sha256": remote["sha256"],
        "etag": etag,
        "status": "synced",
    }


def _mark_own_conflict(
    state: dict[str, Any],
    identity: dict[str, Any],
    *,
    pending_sha256: str,
    actual_revision: int | None,
    remote_sha256: str = "",
) -> None:
    own = state.setdefault("own", {})
    own.update(
        {
            "silicon_id": identity["silicon_id"],
            "status": "conflict",
            "pending_sha256": pending_sha256,
        }
    )
    existing_revision = own.get("conflict_revision")
    valid_actual = (
        isinstance(actual_revision, int)
        and not isinstance(actual_revision, bool)
        and actual_revision >= 0
    )
    valid_existing = (
        isinstance(existing_revision, int)
        and not isinstance(existing_revision, bool)
        and existing_revision >= 0
    )
    if valid_actual and (not valid_existing or actual_revision >= existing_revision):
        own["conflict_revision"] = actual_revision
    if (
        _SHA256_RE.fullmatch(remote_sha256)
        and valid_actual
        and own.get("conflict_revision") == actual_revision
    ):
        own["conflict_sha256"] = remote_sha256


def _own_conflict_result(own: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "status": "conflict",
        "changed": False,
        "local_saved": True,
    }
    actual_revision = own.get("conflict_revision")
    if isinstance(actual_revision, int) and not isinstance(actual_revision, bool):
        result["actual_revision"] = actual_revision
    return result


def _upload_own(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
    content: str,
    expected_revision: int,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Bind verification and mutation to one credential snapshot. If
    # .glass.json rotates between these two requests, the PUT still uses the
    # exact key whose identity was just verified.
    if config is None:
        config, server_origin = _load_config_snapshot(root)
    else:
        server_origin = _validated_server_origin(authenticated_server_url(config))
    live_identity = _fetch_identity(
        root,
        config=config,
        server_origin=server_origin,
    )
    if live_identity != identity:
        _transition_identity(root, state, live_identity)
        raise TeamContextIdentityChanged(
            "Silicon identity changed before advertising-memory upload."
        )
    response = _request(
        root,
        "PUT",
        "/api/v1/silicons/me/advertising-memory",
        body={
            "content": content,
            "expected_revision": expected_revision,
        },
        config=config,
        timeout=12,
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 409:
        try:
            body = _response_json(response, "advertising-memory conflict")
        except TeamContextError:
            body = {}
        actual_revision = body.get("actual_revision")
        if isinstance(actual_revision, bool) or not isinstance(actual_revision, int):
            actual_revision = None
        local_digest = _sha256(content.encode("utf-8"))
        remote_digest = ""
        try:
            remote, response_etag = _fetch_own_remote(
                root,
                identity,
                config=config,
            )
            actual_revision = remote["revision"]
            remote_digest = remote["sha256"]
            if remote_digest == local_digest and remote["content"] == content:
                _set_own_base(state, identity, remote, etag=response_etag)
                return {
                    "ok": True,
                    "status": "unchanged",
                    "changed": False,
                    "local_saved": True,
                    "revision": remote["revision"],
                }
        except Exception as exc:
            if _is_authoritative_access_failure(exc):
                raise
            # The 409 itself is authoritative enough to suppress blind retries;
            # a later conditional reconciliation will refresh missing metadata.
            pass
        _mark_own_conflict(
            state,
            identity,
            pending_sha256=local_digest,
            actual_revision=actual_revision,
            remote_sha256=remote_digest,
        )
        return _own_conflict_result(state["own"])
    _expect_status(response, {200}, "own advertising-memory update")
    payload = _response_json(response, "own advertising-memory update")
    remote = _validate_memory_payload(payload, identity["silicon_id"])
    local_digest = _sha256(content.encode("utf-8"))
    if remote["sha256"] != local_digest or remote["content"] != content:
        raise TeamContextError(
            "Glass acknowledged different advertising-memory content."
        )
    _set_own_base(state, identity, remote, etag=_etag(response))
    return {
        "ok": True,
        "status": "uploaded" if payload.get("changed") is not False else "unchanged",
        "changed": payload.get("changed") is not False,
        "local_saved": True,
        "revision": remote["revision"],
    }


def _upload_explicit_own(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
    content: str,
    *,
    resolve_conflict: bool,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CAS an intentional manager edit without ever replacing its local draft."""

    own = state.get("own") if isinstance(state.get("own"), dict) else {}
    base_revision = own.get("base_revision")
    base_digest = str(own.get("base_sha256") or "")
    has_base = (
        own.get("silicon_id") == identity["silicon_id"]
        and isinstance(base_revision, int)
        and not isinstance(base_revision, bool)
        and base_revision >= 0
        and bool(_SHA256_RE.fullmatch(base_digest))
    )
    local_digest = _sha256(content.encode("utf-8"))

    if own.get("status") == "conflict" and not resolve_conflict:
        _mark_own_conflict(
            state,
            identity,
            pending_sha256=local_digest,
            actual_revision=own.get("conflict_revision"),
            remote_sha256=str(own.get("conflict_sha256") or ""),
        )
        return _own_conflict_result(state["own"])

    if resolve_conflict:
        remote, response_etag = _fetch_own_remote(
            root,
            identity,
            config=config,
        )
        if remote["sha256"] == local_digest and remote["content"] == content:
            _set_own_base(state, identity, remote, etag=response_etag)
            return {
                "ok": True,
                "status": "unchanged",
                "changed": False,
                "local_saved": True,
                "revision": remote["revision"],
            }
        return _upload_own(
            root,
            state,
            identity,
            content,
            remote["revision"],
            config=config,
        )

    if has_base:
        # Even an edit equal to the old base is sent. If another writer changed
        # Glass after that base, expected_revision produces a conflict rather
        # than allowing reconciliation to discard this explicit local choice.
        return _upload_own(
            root,
            state,
            identity,
            content,
            base_revision,
            config=config,
        )

    remote, response_etag = _fetch_own_remote(
        root,
        identity,
        config=config,
    )
    if remote["sha256"] == local_digest:
        _set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "unchanged",
            "changed": False,
            "local_saved": True,
            "revision": remote["revision"],
        }
    if remote["revision"] == 0 and remote["sha256"] == _EMPTY_SHA256:
        return _upload_own(
            root,
            state,
            identity,
            content,
            remote["revision"],
            config=config,
        )

    _mark_own_conflict(
        state,
        identity,
        pending_sha256=local_digest,
        actual_revision=remote["revision"],
        remote_sha256=remote["sha256"],
    )
    return _own_conflict_result(state["own"])


def _sync_own(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
    manifest_entry: dict[str, Any] | None,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = _advertising_file(root, identity["silicon_id"])
    own = state.get("own") if isinstance(state.get("own"), dict) else {}
    if own.get("silicon_id") != identity["silicon_id"]:
        own = {}
        state["own"] = own

    if not path.exists():
        remote, response_etag = _fetch_own_remote(
            root,
            identity,
            config=config,
        )
        if manifest_entry and (
            remote["revision"] != manifest_entry["revision"]
            or remote["sha256"] != manifest_entry["sha256"]
        ):
            raise TeamContextError("Glass own memory does not match the team manifest.")
        _atomic_write_bytes(root, path, remote["content"].encode("utf-8"))
        _set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "downloaded",
            "changed": True,
            "local_saved": True,
            "revision": remote["revision"],
        }

    try:
        local_content, local_digest = _read_local_memory(root, path)
    except ValueError as exc:
        own["status"] = "invalid"
        own["pending_sha256"] = ""
        return {
            "ok": False,
            "status": "invalid",
            "changed": False,
            "local_saved": True,
            "detail": str(exc),
        }

    base_revision = own.get("base_revision")
    base_digest = str(own.get("base_sha256") or "")
    has_base = (
        isinstance(base_revision, int)
        and not isinstance(base_revision, bool)
        and base_revision >= 0
        and bool(_SHA256_RE.fullmatch(base_digest))
    )

    remote: dict[str, Any] | None = None
    response_etag = ""
    if manifest_entry is not None:
        remote_revision = manifest_entry["revision"]
        remote_digest = manifest_entry["sha256"]
    else:
        remote, response_etag = _fetch_own_remote(
            root,
            identity,
            config=config,
        )
        remote_revision = remote["revision"]
        remote_digest = remote["sha256"]

    if own.get("status") == "conflict":
        conflict_revision = own.get("conflict_revision")
        remote_is_current_enough = (
            not isinstance(conflict_revision, int)
            or isinstance(conflict_revision, bool)
            or remote_revision >= conflict_revision
        )
        if local_digest == remote_digest and remote_is_current_enough:
            if remote is None:
                remote = {**manifest_entry, "content": local_content}
            _set_own_base(state, identity, remote, etag=response_etag)
            return {
                "ok": True,
                "status": "unchanged",
                "changed": False,
                "local_saved": True,
                "revision": remote_revision,
            }
        _mark_own_conflict(
            state,
            identity,
            pending_sha256=local_digest,
            actual_revision=remote_revision,
            remote_sha256=remote_digest,
        )
        return _own_conflict_result(state["own"])

    if not has_base:
        if local_digest == remote_digest:
            if remote is None:
                remote = {**manifest_entry, "content": local_content}
            _set_own_base(state, identity, remote, etag=response_etag)
            return {
                "ok": True,
                "status": "unchanged",
                "changed": False,
                "local_saved": True,
                "revision": remote_revision,
            }
        if remote_revision == 0 and remote_digest == _EMPTY_SHA256:
            return _upload_own(
                root,
                state,
                identity,
                local_content,
                remote_revision,
                config=config,
            )
        own["status"] = "conflict"
        own["pending_sha256"] = local_digest
        return {
            "ok": False,
            "status": "conflict",
            "changed": False,
            "local_saved": True,
            "actual_revision": remote_revision,
        }

    local_changed = local_digest != base_digest
    remote_content_changed = remote_digest != base_digest

    if local_digest == remote_digest:
        if remote is None:
            remote = {**manifest_entry, "content": local_content}
        _set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "unchanged",
            "changed": False,
            "local_saved": True,
            "revision": remote_revision,
        }

    if not local_changed and remote_content_changed:
        if remote is None:
            remote, response_etag = _fetch_own_remote(
                root,
                identity,
                config=config,
            )
            if (
                remote["revision"] != remote_revision
                or remote["sha256"] != remote_digest
            ):
                raise TeamContextError(
                    "Glass own memory changed during reconciliation."
                )
        _atomic_write_bytes(root, path, remote["content"].encode("utf-8"))
        _set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "downloaded",
            "changed": True,
            "local_saved": True,
            "revision": remote_revision,
        }

    if local_changed and not remote_content_changed:
        # A revision-only change with identical remote content is safe to adopt
        # before CAS; the local semantic change remains the only content change.
        expected_revision = remote_revision
        return _upload_own(
            root,
            state,
            identity,
            local_content,
            expected_revision,
            config=config,
        )

    if not local_changed:
        # Only the remote revision changed while the content remained identical.
        if remote is None:
            remote = {**manifest_entry, "content": local_content}
        _set_own_base(state, identity, remote, etag=response_etag)
        return {
            "ok": True,
            "status": "unchanged",
            "changed": False,
            "local_saved": True,
            "revision": remote_revision,
        }

    own["status"] = "conflict"
    own["pending_sha256"] = local_digest
    return {
        "ok": False,
        "status": "conflict",
        "changed": False,
        "local_saved": True,
        "actual_revision": remote_revision,
    }


def _prune_stale_peers(
    root: Path,
    old_ids: set[str],
    current_ids: set[str],
    old_records: dict[str, Any],
) -> tuple[int, list[str]]:
    removed = 0
    errors: list[str] = []
    for silicon_id in sorted(old_ids - current_ids):
        record = old_records.get(silicon_id)
        if not isinstance(record, dict):
            continue
        try:
            # Only a fully validated record written by an earlier successful
            # peer fetch grants deletion authority over the fixed local path.
            validated_record = _manifest_entry(record)
            if validated_record["silicon_id"] != silicon_id:
                continue
        except TeamContextError:
            continue
        try:
            path = _advertising_file(root, silicon_id)
            _assert_local_path(root, path)
            existed = os.path.lexists(path)
            if existed:
                _write_visibility_block(root)
            path.unlink(missing_ok=True)
            fsync_directory(path.parent)
            removed += int(existed)
        except (OSError, TeamContextError):
            errors.append(silicon_id)
    return removed, errors


def _archive_unsynced_own_draft(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
) -> bool:
    """Privately preserve an unpublished draft before its path changes role."""

    path = _advertising_file(root, identity["silicon_id"])
    try:
        content, local_digest = _read_local_memory(root, path)
    except FileNotFoundError:
        return False
    except ValueError:
        # A regular file that exceeds the publication contract (including
        # invalid UTF-8) is still the former Silicon's work. Move it into the
        # private runtime archive before the same public path can become a peer
        # mirror. Symlinks and other special files are never followed.
        _assert_local_path(root, path)
        try:
            file_stat = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(file_stat.st_mode):
            return False

        archive_directory = (
            root / DRAFT_ARCHIVE_DIRECTORY / identity["silicon_id"]
        )
        _ensure_private_archive_directory(root, archive_directory)
        fd, archive_name = tempfile.mkstemp(
            prefix="invalid-",
            suffix=".md",
            dir=str(archive_directory),
        )
        os.close(fd)
        archive_path = Path(archive_name)
        moved = False
        try:
            _assert_local_path(root, path)
            _ensure_private_archive_directory(root, archive_directory)
            _assert_local_path(root, archive_path)
            os.replace(path, archive_path)
            moved = True
            try:
                archive_path.chmod(0o600)
            except OSError:
                pass
            if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
                directory_fd = os.open(
                    archive_directory,
                    os.O_RDONLY | os.O_DIRECTORY,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            fsync_directory(path.parent)
        except Exception:
            if not moved:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        archive_relative = archive_path.relative_to(root)
        metadata = {
            "silicon_id": identity["silicon_id"],
            "server_origin": identity["server_origin"],
            "path": archive_relative.as_posix(),
            "validation_status": "invalid",
            "byte_count": file_stat.st_size,
            "archived_at": time.time(),
        }
        _record_draft_archive(state, metadata)
        return True

    own = state.get("own") if isinstance(state.get("own"), dict) else {}
    base_digest = str(own.get("base_sha256") or "")
    has_base = own.get("silicon_id") == identity["silicon_id"] and bool(
        _SHA256_RE.fullmatch(base_digest)
    )
    if has_base and local_digest == base_digest:
        return False

    archive_relative = (
        Path(DRAFT_ARCHIVE_DIRECTORY) / identity["silicon_id"] / f"{local_digest}.md"
    )
    archive_path = root / archive_relative
    _ensure_private_archive_directory(root, archive_path.parent)
    _atomic_write_bytes(root, archive_path, content.encode("utf-8"))

    metadata = {
        "silicon_id": identity["silicon_id"],
        "server_origin": identity["server_origin"],
        "path": archive_relative.as_posix(),
        "sha256": local_digest,
        "archived_at": time.time(),
    }
    _record_draft_archive(state, metadata)
    return True


def _record_draft_archive(
    state: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    archives = state.setdefault("draft_archives", [])
    if not isinstance(archives, list):
        archives = []
        state["draft_archives"] = archives
    archives[:] = [
        item
        for item in archives
        if not (isinstance(item, dict) and item.get("path") == metadata["path"])
    ]
    archives.append(metadata)


def _protect_new_own_scope(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
) -> None:
    """Require an explicit choice before publishing a pre-existing local file.

    The fixed advertising path can survive a Glass-origin or Silicon-ID change.
    Its contents belong to the old authority (or may be an old peer mirror), so
    an empty memory on the new authority must not turn a fallback reconcile into
    an implicit cross-authority publication.
    """

    path = _advertising_file(root, identity["silicon_id"])
    try:
        _content, local_digest = _read_local_memory(root, path)
    except FileNotFoundError:
        return
    except ValueError:
        state["own"] = {
            "silicon_id": identity["silicon_id"],
            "status": "invalid",
            "pending_sha256": "",
            "scope_changed": True,
        }
        return

    state["own"] = {
        "silicon_id": identity["silicon_id"],
        "status": "conflict",
        "pending_sha256": local_digest,
        "scope_changed": True,
    }


def _archive_explicit_draft(
    root: Path,
    state: dict[str, Any],
    content: str,
    *,
    identity: dict[str, Any] | None,
    server_origin: str = "",
) -> None:
    """Preserve an explicit edit privately when no principal can be verified."""

    raw = content.encode("utf-8")
    digest = _sha256(raw)
    silicon_id = identity["silicon_id"] if identity is not None else ""
    archive_scope = silicon_id or "unverified"
    archive_relative = Path(DRAFT_ARCHIVE_DIRECTORY) / archive_scope / f"{digest}.md"
    archive_path = root / archive_relative
    _ensure_private_archive_directory(root, archive_path.parent)
    _atomic_write_bytes(root, archive_path, raw)
    _record_draft_archive(
        state,
        {
            "silicon_id": silicon_id,
            "server_origin": (
                identity["server_origin"] if identity is not None else server_origin
            ),
            "path": archive_relative.as_posix(),
            "sha256": digest,
            "validation_status": "valid",
            "reason": "identity_unverified",
            "archived_at": time.time(),
        },
    )


def _quarantine_unscoped_advertising_files(
    root: Path,
    state: dict[str, Any],
    *,
    preserve_ids: set[str],
) -> int:
    """Remove strict advertising paths whose Glass provenance was lost."""

    directory = root / ADVERTISING_DIRECTORY
    _assert_local_path(root, directory)
    try:
        directory_stat = os.stat(directory, follow_symlinks=False)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise TeamContextError(
                "Advertising-memory directory must be a local directory."
            )
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return 0

    archive_directory = root / DRAFT_ARCHIVE_DIRECTORY / "unscoped"
    moved = 0
    for entry in entries:
        if entry.suffix != ".md":
            continue
        try:
            silicon_id = _validate_identifier(entry.stem, "Silicon ID")
        except TeamContextError:
            continue
        if silicon_id in preserve_ids or entry.name != f"{silicon_id}.md":
            continue

        _assert_local_path(root, entry)
        entry_stat = os.stat(entry, follow_symlinks=False)
        if stat.S_ISLNK(entry_stat.st_mode):
            entry.unlink(missing_ok=True)
            fsync_directory(entry.parent)
            moved += 1
            continue

        _ensure_private_archive_directory(root, archive_directory)
        nonce = 0
        while True:
            archive_path = archive_directory / (
                f"{silicon_id}-{time.time_ns()}-{nonce}.quarantine"
            )
            if not os.path.lexists(archive_path):
                break
            nonce += 1
        _assert_local_path(root, entry)
        _ensure_private_archive_directory(root, archive_directory)
        _assert_local_path(root, archive_path)
        os.replace(entry, archive_path)
        fsync_directory(entry.parent)
        fsync_directory(archive_directory)
        if stat.S_ISREG(entry_stat.st_mode):
            try:
                archive_path.chmod(0o600)
            except OSError:
                pass
        archive_relative = archive_path.relative_to(root)
        _record_draft_archive(
            state,
            {
                "silicon_id": silicon_id,
                "server_origin": "",
                "path": archive_relative.as_posix(),
                "validation_status": "unscoped",
                "byte_count": (
                    entry_stat.st_size if stat.S_ISREG(entry_stat.st_mode) else None
                ),
                "reason": "state_provenance_missing",
                "archived_at": time.time(),
            },
        )
        moved += 1
    return moved


def _invalidate_team_visibility(
    root: Path,
    state: dict[str, Any],
    *,
    preserve_ids: set[str] | None = None,
) -> int:
    """Fail closed while deleting only peer paths previously managed by Glass."""

    preserve_ids = preserve_ids or set()
    try:
        _write_visibility_block(root)
    except OSError:
        # Context state and TEAM.md deletion below remain the primary boundary;
        # this marker closes the state-save/file-lock failure window.
        pass
    old_records = (
        dict(state.get("peers")) if isinstance(state.get("peers"), dict) else {}
    )
    old_ids = {
        item for item in state.get("managed_peer_ids", []) if isinstance(item, str)
    }
    removed, errors = _prune_stale_peers(
        root,
        old_ids,
        preserve_ids,
        old_records,
    )
    retained: dict[str, Any] = {}
    for silicon_id in errors:
        record = old_records.get(silicon_id)
        if isinstance(record, dict):
            retained[silicon_id] = record
    state["context"] = {}
    state["peers"] = retained
    state["managed_peer_ids"] = sorted(retained)
    try:
        _write_team_placeholder(root)
    except OSError:
        # Clearing the verified revision is sufficient for prompt reads to fail
        # closed even when the generated file is temporarily locked.
        pass
    return removed


def _mark_configured_scope_change(
    root: Path,
    state: dict[str, Any],
    server_origin: str,
    credential_fingerprint: str,
) -> bool:
    previous = _stored_identity(state)
    if previous is None or (
        previous["server_origin"] == server_origin
        and previous["credential_fingerprint"] == credential_fingerprint
    ):
        return False
    _invalidate_team_visibility(
        root,
        state,
        preserve_ids={previous["silicon_id"]},
    )
    state["identity"] = {**previous, "access_valid": False}
    return True


def _transition_identity(
    root: Path,
    state: dict[str, Any],
    identity: dict[str, Any],
) -> tuple[bool, int]:
    previous = _stored_identity(state)
    canonical = {
        "silicon_id": _validate_identifier(identity.get("silicon_id"), "Silicon ID"),
        "team_slug": _validate_identifier(identity.get("team_slug"), "team slug"),
        "server_origin": _validated_server_origin(identity.get("server_origin")),
        "credential_fingerprint": _validated_credential_fingerprint(
            identity.get("credential_fingerprint")
        ),
        "access_valid": True,
    }
    if previous == canonical:
        return False, 0

    preserve_ids = {canonical["silicon_id"]}
    if previous is not None:
        preserve_ids.add(previous["silicon_id"])
    removed = _invalidate_team_visibility(
        root,
        state,
        preserve_ids=preserve_ids,
    )
    if previous is not None:
        # A verified transition must never leave the former identity available
        # as an offline-draft fallback if the remaining local work fails.
        state["identity"] = {**previous, "access_valid": False}
        if (
            previous["silicon_id"] != canonical["silicon_id"]
            or previous["server_origin"] != canonical["server_origin"]
        ):
            # If this write fails, do not accept the new identity in state and
            # do not proceed far enough to overwrite the former owner's path.
            _archive_unsynced_own_draft(root, state, previous)
            if previous["silicon_id"] != canonical["silicon_id"]:
                # The old public path is no longer this process's own memory.
                # It may be recreated only from the new team's authenticated
                # peer manifest; unpublished work lives solely in the private
                # archive created above.
                old_path = _advertising_file(root, previous["silicon_id"])
                _assert_local_path(root, old_path)
                old_path.unlink(missing_ok=True)
                fsync_directory(old_path.parent)

    principal_changed = previous is not None and (
        previous["silicon_id"] != canonical["silicon_id"]
        or previous["server_origin"] != canonical["server_origin"]
    )
    if previous is None:
        _quarantine_unscoped_advertising_files(
            root,
            state,
            preserve_ids={canonical["silicon_id"]},
        )
    changed = _set_identity(state, canonical)
    if principal_changed or previous is None:
        _protect_new_own_scope(root, state, canonical)
    return changed, removed


def _is_authoritative_access_failure(exc: BaseException) -> bool:
    return isinstance(exc, TeamContextError) and exc.status_code in {401, 403, 404}


def _invalidate_team_access(root: Path, state: dict[str, Any]) -> None:
    previous = _stored_identity(state)
    preserve_ids = {previous["silicon_id"]} if previous is not None else set()
    _invalidate_team_visibility(root, state, preserve_ids=preserve_ids)
    if previous is None:
        state["identity"] = {}
    else:
        state["identity"] = {**previous, "access_valid": False}


def _record_reconcile_success(
    state: dict[str, Any],
    *,
    now: float,
    partial: bool,
) -> None:
    state["schedule"] = {
        "last_reconcile_at": now,
        "last_attempt_at": now,
        "next_reconcile_at": now + (10 if partial else RECONCILE_INTERVAL_SECONDS),
        "failure_count": 0,
    }


def _record_reconcile_failure(state: dict[str, Any], *, now: float) -> None:
    schedule = state.setdefault("schedule", {})
    raw_failures = schedule.get("failure_count")
    failures = (
        raw_failures
        if isinstance(raw_failures, int)
        and not isinstance(raw_failures, bool)
        and raw_failures >= 0
        else 0
    ) + 1
    delay = min(RECONCILE_INTERVAL_SECONDS, max(10, 2 ** min(failures, 6)))
    schedule.update(
        {
            "last_attempt_at": now,
            "next_reconcile_at": now + delay,
            "failure_count": failures,
        }
    )


def _reconcile_locked(
    root: Path,
    state: dict[str, Any],
    *,
    force: bool,
    reason: str,
    config: dict[str, Any] | None = None,
    server_origin: str = "",
) -> dict[str, Any]:
    del reason  # Kept out of persistent state and logs; callers receive status only.
    if config is None:
        config, configured_origin = _load_config_snapshot(root)
        server_origin = server_origin or configured_origin
    else:
        server_origin = server_origin or authenticated_server_url(config)
    server_origin = _validated_server_origin(server_origin)
    credential_fingerprint = _credential_fingerprint(config)
    previous_peers = (
        dict(state.get("peers")) if isinstance(state.get("peers"), dict) else {}
    )
    if _stored_identity(state) is None and state.get("context"):
        # State written before origin scoping (or with corrupt identity fields)
        # cannot authorize prompt-visible data from an unknown Glass server.
        _invalidate_team_visibility(root, state)
    scope_changed = _mark_configured_scope_change(
        root,
        state,
        server_origin,
        credential_fingerprint,
    )
    identity = _fetch_identity(
        root,
        config=config,
        server_origin=server_origin,
    )
    identity_changed, transition_removed = _transition_identity(
        root,
        state,
        identity,
    )
    identity_changed = identity_changed or scope_changed

    response = _get_context_response(
        root,
        identity,
        state,
        force=force or identity_changed,
        config=config,
    )
    status = _expect_status(response, {200, 304}, "team-context request")
    context_changed = False

    if status == 304:
        context_state = state.get("context") or {}
        revision = str(context_state.get("revision") or "")
        try:
            manifest = _manifest_from_state(state)
        except TeamContextError:
            manifest = {}
        if (
            not manifest
            or not _SHA256_RE.fullmatch(revision)
            or not _team_file_matches(root, revision)
            or not _context_matches_identity_scope(state, identity)
        ):
            response = _get_context_response(
                root,
                identity,
                state,
                force=True,
                config=config,
            )
            _expect_status(response, {200}, "team-context repair request")
            status = 200

    if status == 200:
        payload = _response_json(response, "team-context request")
        context, manifest = _validate_context_payload(payload, identity)
        context_etag = _etag(response)
        if not _team_file_matches(root, context["revision"]):
            _atomic_write_bytes(
                root, _team_file(root), context["markdown"].encode("utf-8")
            )
            context_changed = True
        state["context"] = {
            **{key: value for key, value in context.items() if key != "markdown"},
            "etag": context_etag,
            "server_origin": server_origin,
            "credential_fingerprint": identity["credential_fingerprint"],
            "advertising_memories": _manifest_for_state(manifest),
        }
    else:
        manifest = _manifest_from_state(state)

    own_id = identity["silicon_id"]
    old_managed = {
        str(item) for item in state.get("managed_peer_ids", []) if isinstance(item, str)
    }
    current_peer_ids = set(manifest) - {own_id}
    old_peers = (
        {}
        if identity_changed
        else state.get("peers")
        if isinstance(state.get("peers"), dict)
        else {}
    )
    peer_ids = sorted(current_peer_ids)
    new_peers: dict[str, dict[str, Any]] = {}
    peer_changed = 0
    unverified_removed = 0
    errors: list[str] = []
    if peer_ids:
        # Grant deletion authority in memory before workers can publish peer
        # files. If any concurrent request proves that access was revoked, the
        # outer authoritative-failure handler can remove every file written by
        # this batch rather than leaking a newly downloaded, untracked peer.
        provisional_peers: dict[str, dict[str, Any]] = {}
        for silicon_id in peer_ids:
            old_record = old_peers.get(silicon_id)
            provisional_peers[silicon_id] = {
                **manifest[silicon_id],
                "etag": (
                    str(old_record.get("etag") or "")
                    if isinstance(old_record, dict)
                    else ""
                ),
            }
        state["peers"] = provisional_peers
        state["managed_peer_ids"] = peer_ids

        peer_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=min(MAX_PARALLEL_PEER_SYNCS, len(peer_ids)),
            thread_name_prefix="team-peer-sync",
        )
        peer_futures: dict[
            Future[tuple[dict[str, Any], bool, bytes | None]],
            str,
        ] = {}
        peer_outcomes: dict[
            str,
            tuple[dict[str, Any], bool, bytes | None] | BaseException,
        ] = {}
        try:
            for silicon_id in peer_ids:
                old_record = old_peers.get(silicon_id)
                future = peer_executor.submit(
                    _sync_peer,
                    root,
                    identity,
                    manifest[silicon_id],
                    old_record if isinstance(old_record, dict) else {},
                    config=config,
                )
                peer_futures[future] = silicon_id

            # Detect a revoked credential in completion order so a slow request
            # for an alphabetically earlier peer cannot delay fail-closed
            # handling. Workers only stage validated bytes; they never publish
            # to the shared prompt tree.
            for future in as_completed(peer_futures):
                silicon_id = peer_futures[future]
                try:
                    peer_outcomes[silicon_id] = future.result()
                except Exception as exc:
                    if _is_authoritative_access_failure(exc):
                        for pending in peer_futures:
                            pending.cancel()
                        peer_executor.shutdown(
                            wait=False,
                            cancel_futures=True,
                        )
                        peer_executor = None
                        raise
                    peer_outcomes[silicon_id] = exc
        finally:
            if peer_executor is not None:
                peer_executor.shutdown(wait=True)

        # State and filesystem publication remain deterministic even though the
        # network fetches above completed out of order.
        for silicon_id in peer_ids:
            outcome = peer_outcomes[silicon_id]
            if isinstance(outcome, BaseException):
                errors.append(silicon_id)
                old_record = old_peers.get(silicon_id)
                if (
                    isinstance(old_record, dict)
                    and _peer_file_matches_record(
                        root,
                        silicon_id,
                        old_record,
                    )
                ):
                    new_peers[silicon_id] = old_record
                else:
                    try:
                        unverified_removed += int(
                            _remove_unverified_peer_file(root, silicon_id)
                        )
                    except (OSError, TeamContextError) as exc:
                        _invalidate_team_visibility(
                            root,
                            state,
                            preserve_ids={own_id},
                        )
                        raise TeamContextError(
                            "Could not hide an unverified peer advertising mirror."
                        ) from exc
                continue
            record, changed, staged_content = outcome
            try:
                if staged_content is not None:
                    _atomic_write_bytes(
                        root,
                        _advertising_file(root, silicon_id),
                        staged_content,
                    )
                new_peers[silicon_id] = record
                peer_changed += int(changed)
            except Exception:
                errors.append(silicon_id)
                old_record = old_peers.get(silicon_id)
                try:
                    _content, published_digest = _read_local_memory(
                        root,
                        _advertising_file(root, silicon_id),
                        allow_managed=True,
                    )
                except (OSError, ValueError):
                    published_digest = ""
                if published_digest == record["sha256"]:
                    # os.replace may have committed before a later directory
                    # fsync failed. Keep deletion authority for those verified
                    # bytes even though this pass remains partial.
                    new_peers[silicon_id] = record
                    peer_changed += int(changed)
                elif (
                    isinstance(old_record, dict)
                    and _peer_file_matches_record(
                        root,
                        silicon_id,
                        old_record,
                    )
                ):
                    new_peers[silicon_id] = old_record
                else:
                    try:
                        unverified_removed += int(
                            _remove_unverified_peer_file(root, silicon_id)
                        )
                    except (OSError, TeamContextError) as exc:
                        _invalidate_team_visibility(
                            root,
                            state,
                            preserve_ids={own_id},
                        )
                        raise TeamContextError(
                            "Could not hide an unverified peer advertising mirror."
                        ) from exc

    try:
        own_result = _sync_own(
            root,
            state,
            identity,
            manifest.get(own_id),
            config=config,
        )
    except TeamContextIdentityChanged:
        raise
    except Exception as exc:
        if _is_authoritative_access_failure(exc):
            raise
        own_result = {
            "ok": False,
            "status": "unavailable",
            "changed": False,
            "local_saved": _advertising_file(root, own_id).exists(),
        }
        errors.append(own_id)

    removed, prune_errors = _prune_stale_peers(
        root,
        old_managed - {own_id},
        current_peer_ids,
        previous_peers,
    )
    removed += unverified_removed
    removed += transition_removed
    errors.extend(prune_errors)
    for silicon_id in prune_errors:
        old_record = previous_peers.get(silicon_id)
        if isinstance(old_record, dict):
            new_peers[silicon_id] = old_record
    state["peers"] = new_peers
    state["managed_peer_ids"] = sorted(current_peer_ids | set(prune_errors))
    own_ok = bool(own_result.get("ok", False))
    own_status = str(own_result.get("status") or "unavailable")
    own_detail = str(own_result.get("detail") or "").strip()
    has_issue = bool(errors) or not own_ok
    retry_soon = bool(errors) or (
        not own_ok and own_status not in {"conflict", "invalid"}
    )
    any_changed = bool(
        context_changed or peer_changed or removed or own_result.get("changed")
    )
    _record_reconcile_success(
        state,
        now=time.time(),
        partial=retry_soon,
    )
    result = {
        "ok": not has_issue,
        "status": "partial" if has_issue else ("updated" if any_changed else "current"),
        "changed": any_changed,
        "context_changed": context_changed,
        "peer_files_changed": peer_changed,
        "peer_files_removed": removed,
        "own_status": own_status,
        "error_count": len(errors),
    }
    if own_detail:
        result["own_detail"] = own_detail
    return result


def _safe_failure(
    status: str,
    *,
    local_saved: bool = False,
    detail: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "status": status,
        "changed": False,
    }
    if local_saved:
        result["local_saved"] = True
    if detail:
        result["detail"] = detail
    return result


def _managed_peer_ids(state: dict[str, Any]) -> set[str]:
    managed: set[str] = set()
    for raw in state.get("managed_peer_ids", []):
        try:
            managed.add(_validate_identifier(raw, "Silicon ID"))
        except TeamContextError:
            continue
    return managed


def _rollback_uncommitted_peer_files(
    root: Path,
    state: dict[str, Any],
    *,
    previous_identity: dict[str, Any] | None,
    previous_managed_ids: set[str],
) -> None:
    """Remove peer files whose deletion authority failed to reach disk."""

    current_identity = _stored_identity(state)
    current_managed_ids = _managed_peer_ids(state)
    if previous_identity != current_identity:
        rollback_ids = current_managed_ids
    else:
        rollback_ids = current_managed_ids - previous_managed_ids
    current_own_id = (
        current_identity["silicon_id"] if current_identity is not None else ""
    )
    for silicon_id in sorted(rollback_ids - {current_own_id}):
        try:
            rollback_path = _advertising_file(root, silicon_id)
            _assert_local_path(root, rollback_path)
            rollback_path.unlink(missing_ok=True)
            fsync_directory(rollback_path.parent)
        except (OSError, TeamContextError):
            pass


def reconcile_team_context(
    root: str | Path | None = None,
    *,
    force: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Reconcile TEAM.md and all advertising mirrors with Glass.

    ``force`` bypasses the team-context ETag, which is useful on startup and
    reconnect. Expected configuration, network, authentication, protocol, lock,
    and filesystem failures are swallowed and represented by the returned
    status. Temporary failures preserve last-known-good data. Confirmed access,
    team, Silicon, or Glass-origin changes fail closed immediately.
    """

    project_root = _normalise_root(root)
    try:
        try:
            ensure_team_context_layout(project_root)
        except TeamContextError as exc:
            return _safe_failure("invalid", detail=str(exc))
        with _sync_lock(project_root):
            state = _load_state(project_root)
            previous_identity = _stored_identity(state)
            previous_managed_ids = _managed_peer_ids(state)
            previous_had_context = bool(state.get("context"))
            try:
                previous_team_bytes = _read_regular_bytes(
                    project_root,
                    _team_file(project_root),
                    max_bytes=MAX_TEAM_CONTEXT_BYTES,
                )
            except (OSError, ValueError):
                previous_team_bytes = None
            try:
                result = _reconcile_locked(
                    project_root,
                    state,
                    force=force,
                    reason=reason,
                )
            except Exception as exc:
                if _is_authoritative_access_failure(exc):
                    _invalidate_team_access(project_root, state)
                    result = _safe_failure("unauthorized")
                else:
                    result = _safe_failure("unavailable")
                _record_reconcile_failure(state, now=time.time())
            try:
                _save_state(project_root, state)
            except Exception:
                destructive_peer_change = bool(
                    result.get("peer_files_removed")
                    if isinstance(result, dict)
                    else False
                )
                if destructive_peer_change and not os.path.lexists(
                    _visibility_block_file(project_root)
                ):
                    try:
                        _write_visibility_block(project_root)
                    except Exception:
                        pass
                _rollback_uncommitted_peer_files(
                    project_root,
                    state,
                    previous_identity=previous_identity,
                    previous_managed_ids=previous_managed_ids,
                )
                current_identity = _stored_identity(state)
                must_fail_closed = (
                    destructive_peer_change
                    or previous_identity != current_identity
                    or (previous_had_context and not state.get("context"))
                )
                try:
                    if previous_team_bytes is None or must_fail_closed:
                        _write_team_placeholder(project_root)
                    else:
                        _atomic_write_bytes(
                            project_root,
                            _team_file(project_root),
                            previous_team_bytes,
                        )
                except Exception:
                    pass
                return _safe_failure("state_error")
            if result.get("status") in {"current", "updated", "partial"}:
                _clear_visibility_block_if_verified(project_root, state)
            return result
    except TeamContextLockTimeout:
        return _safe_failure("busy")
    except Exception:
        log.exception("team context reconciliation failed")
        return _safe_failure("unavailable")


def team_context_tick(root: str | Path | None = None) -> dict[str, Any]:
    """Cheap main-loop hook: hash the own file and run the 60-second fallback."""

    project_root = _normalise_root(root)
    try:
        try:
            ensure_team_context_layout(project_root)
        except TeamContextError as exc:
            return _safe_failure("invalid", detail=str(exc))
        with _sync_lock(project_root):
            state = _load_state(project_root)
            previous_identity = _stored_identity(state)
            previous_managed_ids = _managed_peer_ids(state)
            previous_had_context = bool(state.get("context"))
            try:
                previous_team_bytes = _read_regular_bytes(
                    project_root,
                    _team_file(project_root),
                    max_bytes=MAX_TEAM_CONTEXT_BYTES,
                )
            except (OSError, ValueError):
                previous_team_bytes = None
            try:
                config, server_origin = _load_config_snapshot(project_root)
                credential_fingerprint = _credential_fingerprint(config)
                schedule = state.get("schedule") or {}
                next_reconcile_at = _safe_schedule_timestamp(
                    schedule.get("next_reconcile_at"),
                    now=time.time(),
                    allow_future=True,
                )
                identity = _cached_identity(state)
                stored_identity = _stored_identity(state)
                scope_changed = (
                    stored_identity is not None
                    and stored_identity["access_valid"]
                    and (
                        stored_identity["server_origin"] != server_origin
                        or stored_identity["credential_fingerprint"]
                        != credential_fingerprint
                    )
                )
                needs_full_reconcile = (
                    identity is None
                    or scope_changed
                    or not state.get("context")
                    or (
                        identity is not None
                        and not _context_matches_identity_scope(state, identity)
                    )
                    or os.path.lexists(_visibility_block_file(project_root))
                )
                due = scope_changed or time.time() >= next_reconcile_at
                if due:
                    result = _reconcile_locked(
                        project_root,
                        state,
                        force=False,
                        reason="fallback",
                        config=config,
                        server_origin=server_origin,
                    )
                elif needs_full_reconcile:
                    result = _safe_failure("deferred")
                else:
                    manifest = _manifest_from_state(state)
                    if identity is None:
                        raise TeamContextError("No cached Silicon identity.")
                    result = _sync_own(
                        project_root,
                        state,
                        identity,
                        manifest.get(identity["silicon_id"]),
                        config=config,
                    )
            except Exception as exc:
                if _is_authoritative_access_failure(exc):
                    _invalidate_team_access(project_root, state)
                    result = _safe_failure("unauthorized")
                else:
                    result = _safe_failure("unavailable")
                _record_reconcile_failure(state, now=time.time())
            try:
                _save_state(project_root, state)
            except Exception:
                destructive_peer_change = bool(
                    result.get("peer_files_removed")
                    if isinstance(result, dict)
                    else False
                )
                if destructive_peer_change and not os.path.lexists(
                    _visibility_block_file(project_root)
                ):
                    try:
                        _write_visibility_block(project_root)
                    except Exception:
                        pass
                _rollback_uncommitted_peer_files(
                    project_root,
                    state,
                    previous_identity=previous_identity,
                    previous_managed_ids=previous_managed_ids,
                )
                current_identity = _stored_identity(state)
                must_fail_closed = (
                    destructive_peer_change
                    or previous_identity != current_identity
                    or (previous_had_context and not state.get("context"))
                )
                try:
                    if previous_team_bytes is None or must_fail_closed:
                        _write_team_placeholder(project_root)
                    else:
                        _atomic_write_bytes(
                            project_root,
                            _team_file(project_root),
                            previous_team_bytes,
                        )
                except Exception:
                    pass
                return _safe_failure("state_error")
            if result.get("status") in {"current", "updated", "partial"}:
                _clear_visibility_block_if_verified(project_root, state)
            return result
    except TeamContextLockTimeout:
        return _safe_failure("busy")
    except Exception:
        log.exception("team context tick failed")
        return _safe_failure("unavailable")


def update_own_advertising_memory(
    content: str,
    root: str | Path | None = None,
    *,
    resolve_conflict: bool = False,
) -> dict[str, Any]:
    """Save and CAS-upload this authenticated Silicon's own advertising memory.

    The caller cannot supply a path or Silicon ID. Identity comes from Glass,
    with the last server-verified cached identity used only to preserve a local
    draft while Glass is temporarily unavailable. A conflict or failed upload
    never discards the atomically written local content. Normal calls preserve
    an existing conflict without retrying stale CAS state; pass
    ``resolve_conflict=True`` to intentionally replace the latest remote
    revision with this content.
    """

    if not isinstance(resolve_conflict, bool):
        return _safe_failure(
            "invalid",
            detail="resolve_conflict must be a boolean.",
        )
    try:
        validate_advertising_memory(content)
    except ValueError as exc:
        return _safe_failure("invalid", detail=str(exc))

    project_root = _normalise_root(root)
    try:
        try:
            ensure_team_context_layout(project_root)
        except TeamContextError as exc:
            return _safe_failure("invalid", detail=str(exc))
        with _sync_lock(project_root):
            state = _load_state(project_root)
            previous_identity = _stored_identity(state)
            identity: dict[str, Any] | None = None
            config: dict[str, Any] | None = None
            server_origin = ""
            identity_error = False
            result: dict[str, Any] | None = None
            local_saved = False
            try:
                config, server_origin = _load_config_snapshot(project_root)
            except Exception:
                identity_error = True
                identity = _cached_identity(state)
            else:
                try:
                    _mark_configured_scope_change(
                        project_root,
                        state,
                        server_origin,
                        _credential_fingerprint(config),
                    )
                    identity = _fetch_identity(
                        project_root,
                        config=config,
                        server_origin=server_origin,
                    )
                except Exception as exc:
                    identity_error = True
                    if _is_authoritative_access_failure(exc):
                        _invalidate_team_access(project_root, state)
                        archive_identity = (
                            previous_identity
                            if previous_identity is not None
                            and previous_identity["server_origin"] == server_origin
                            else None
                        )
                        try:
                            _archive_explicit_draft(
                                project_root,
                                state,
                                content,
                                identity=archive_identity,
                                server_origin=server_origin,
                            )
                            local_saved = True
                        except Exception:
                            local_saved = False
                        result = _safe_failure(
                            "unauthorized",
                            local_saved=local_saved,
                        )
                    else:
                        cached = _cached_identity(state)
                        if (
                            cached is not None
                            and cached["server_origin"] == server_origin
                            and cached["credential_fingerprint"]
                            == _credential_fingerprint(config)
                        ):
                            identity = cached
                        else:
                            result = _safe_failure("identity_unavailable")
                else:
                    try:
                        _transition_identity(project_root, state, identity)
                    except Exception:
                        identity = None
                        result = _safe_failure("state_error")

            if identity is None and result is None:
                result = _safe_failure("identity_unavailable")

            if identity is None and not local_saved:
                archive_identity = (
                    previous_identity
                    if previous_identity is not None
                    and (
                        not server_origin
                        or previous_identity["server_origin"] == server_origin
                    )
                    else None
                )
                try:
                    _archive_explicit_draft(
                        project_root,
                        state,
                        content,
                        identity=archive_identity,
                        server_origin=server_origin,
                    )
                    local_saved = True
                except Exception:
                    local_saved = False
                if result is None:
                    result = _safe_failure("identity_unavailable")
                result["local_saved"] = local_saved

            if identity is not None:
                own_path = _advertising_file(
                    project_root,
                    identity["silicon_id"],
                )
                _atomic_write_bytes(
                    project_root,
                    own_path,
                    content.encode("utf-8"),
                )
                local_saved = True

                if identity_error:
                    result = _safe_failure("pending", local_saved=True)
                    own = state.setdefault("own", {})
                    own["silicon_id"] = identity["silicon_id"]
                    own["status"] = "pending"
                    own["pending_sha256"] = _sha256(content.encode("utf-8"))
                else:
                    try:
                        result = _upload_explicit_own(
                            project_root,
                            state,
                            identity,
                            content,
                            resolve_conflict=bool(resolve_conflict),
                            config=config,
                        )
                    except TeamContextIdentityChanged:
                        result = _safe_failure(
                            "identity_changed",
                            local_saved=True,
                        )
                    except Exception as exc:
                        if _is_authoritative_access_failure(exc):
                            _invalidate_team_access(project_root, state)
                            result = _safe_failure(
                                "unauthorized",
                                local_saved=True,
                            )
                        else:
                            result = _safe_failure(
                                "pending",
                                local_saved=True,
                            )
                            if _cached_identity(state) == identity:
                                own = state.setdefault("own", {})
                                if own.get("status") != "conflict":
                                    own["silicon_id"] = identity["silicon_id"]
                                    own["status"] = "pending"
                                    own["pending_sha256"] = _sha256(
                                        content.encode("utf-8")
                                    )
            try:
                _save_state(project_root, state)
            except Exception:
                return _safe_failure("state_error", local_saved=local_saved)
            return result or _safe_failure("unavailable", local_saved=local_saved)
    except TeamContextLockTimeout:
        return _safe_failure("busy")
    except Exception:
        log.exception("own advertising-memory update failed")
        return _safe_failure("unavailable")


def read_verified_team_markdown(
    root: str | Path | None = None,
    max_bytes: int = MAX_TEAM_CONTEXT_BYTES,
) -> str:
    """Return only the last Glass-verified TEAM.md bytes, decoded as UTF-8.

    Prompt assembly can call this without taking the synchronization lock:
    generated files and state are replaced atomically, and a revision mismatch
    simply fails closed until the next read. Symlinks, oversized files, invalid
    UTF-8, missing state, and malformed revisions all return an empty string.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        return ""
    max_bytes = min(max_bytes, MAX_TEAM_CONTEXT_BYTES)
    project_root = _normalise_root(root)
    path = _team_file(project_root)
    try:
        if os.path.lexists(_visibility_block_file(project_root)):
            return ""
        config, configured_origin = _load_config_snapshot(project_root)
        configured_fingerprint = _credential_fingerprint(config)
        state = _load_state(project_root)
        identity = _cached_identity(state)
        context = state.get("context") or {}
        if (
            identity is None
            or configured_origin != identity["server_origin"]
            or configured_fingerprint != identity["credential_fingerprint"]
            or context.get("team_slug") != identity["team_slug"]
            or context.get("server_origin") != identity["server_origin"]
            or context.get("credential_fingerprint")
            != identity["credential_fingerprint"]
        ):
            return ""
        revision = str(context.get("revision") or "")
        if not _SHA256_RE.fullmatch(revision):
            return ""
        raw = _read_regular_bytes(project_root, path, max_bytes=max_bytes)
        if _sha256(raw) != revision:
            return ""
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError, TeamContextError):
        return ""


def read_verified_team_advertising_memories(
    root: str | Path | None = None,
    *,
    expected_team_revision: str = "",
) -> list[dict[str, Any]]:
    """Return every locally mirrored advertising memory verified by Glass.

    The team manifest supplies the expected Silicon ID, path, revision, and
    digest. Prompt assembly receives a memory only when the local regular file
    still matches that manifest and the cached identity remains scoped to the
    configured Glass origin and credential. Missing, stale, malformed, or
    locally modified mirrors are omitted rather than exposed.
    """

    project_root = _normalise_root(root)
    try:
        if not read_verified_team_markdown(project_root):
            return []
        if os.path.lexists(_visibility_block_file(project_root)):
            return []
        config, configured_origin = _load_config_snapshot(project_root)
        configured_fingerprint = _credential_fingerprint(config)
        state = _load_state(project_root)
        identity = _cached_identity(state)
        context = state.get("context") or {}
        if (
            identity is None
            or (
                expected_team_revision
                and context.get("revision") != expected_team_revision
            )
            or configured_origin != identity["server_origin"]
            or configured_fingerprint != identity["credential_fingerprint"]
            or context.get("team_slug") != identity["team_slug"]
            or context.get("server_origin") != identity["server_origin"]
            or context.get("credential_fingerprint")
            != identity["credential_fingerprint"]
        ):
            return []
        manifest = _normalise_manifest(context.get("advertising_memories"))
        own_id = identity["silicon_id"]
        if own_id not in manifest:
            return []

        memories: list[dict[str, Any]] = []
        for silicon_id in sorted(manifest):
            entry = manifest[silicon_id]
            try:
                content, digest = _read_local_memory(
                    project_root,
                    _advertising_file(project_root, silicon_id),
                    allow_managed=silicon_id != own_id,
                )
            except (OSError, ValueError, TeamContextError):
                continue
            if digest != entry["sha256"]:
                continue
            memories.append({**entry, "content": content})
        return memories
    except (OSError, ValueError, TeamContextError):
        return []
