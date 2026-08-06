"""Cross-thread/process locking and atomic, durable file persistence.

This module owns every primitive Silicon uses to put bytes on disk safely:
advisory locking, ``fsync``-backed atomic replacement, and the JSON helpers
built on top of them.  Callers that need durability should reach for these
rather than rolling their own temp-file dance.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

try:  # Unix/macOS
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # Windows
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - Unix/macOS
    msvcrt = None


T = TypeVar("T")
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_LOCAL = threading.local()


def _path_key(path: str | os.PathLike[str]) -> str:
    return str(Path(path).expanduser().resolve())


def _thread_lock(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def lock_handle(handle) -> None:
    """Take an exclusive advisory lock on an already-open file handle."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        # msvcrt locks a byte range, so the file needs at least one byte.
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def unlock_handle(handle) -> None:
    """Release the advisory lock taken by :func:`lock_handle`."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def chmod_open_file(descriptor: int, path: Path, mode: int) -> None:
    """Set permissions on an open file, by descriptor where the OS allows it."""
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, mode)
    else:  # pragma: no cover - exercised on Windows
        os.chmod(path, mode)


def fsync_directory(path: Path) -> None:
    """Flush a directory entry so a rename into it survives a crash."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    dir_mode: int | None = 0o700,
) -> None:
    """Replace ``path`` with ``data`` atomically and durably.

    Writes to a sibling temp file, fsyncs it, renames it into place, then
    fsyncs the parent directory so the rename itself is durable.  Pass
    ``dir_mode=None`` to create missing parents with default permissions.
    """
    if dir_mode is None:
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        chmod_open_file(descriptor, temporary, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path: str | os.PathLike[str]) -> Iterator[None]:
    """Take a re-entrant process-local and advisory cross-process lock."""
    key = _path_key(path)
    lock = _thread_lock(key)
    with lock:
        depths = getattr(_LOCAL, "depths", None)
        if depths is None:
            depths = {}
            _LOCAL.depths = depths
        depth = int(depths.get(key, 0))
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        lock_path = Path(f"{key}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            lock_handle(handle)
            depths[key] = 1
            yield
        finally:
            depths.pop(key, None)
            try:
                unlock_handle(handle)
            finally:
                handle.close()


def _read_json_unlocked(path: Path, default: T) -> T:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def read_json(path: str | os.PathLike[str], default: T) -> T:
    resolved = Path(path)
    with file_lock(resolved):
        return _read_json_unlocked(resolved, default)


def _write_json_unlocked(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_json(path: str | os.PathLike[str], data: Any) -> None:
    resolved = Path(path)
    with file_lock(resolved):
        _write_json_unlocked(resolved, data)


def update_json(
    path: str | os.PathLike[str],
    default: T,
    update: Callable[[T], Any],
) -> Any:
    """Atomically read, mutate, and replace a JSON document."""
    resolved = Path(path)
    with file_lock(resolved):
        current = _read_json_unlocked(resolved, default)
        result = update(current)
        _write_json_unlocked(resolved, current)
        return result


def update_json_if_changed(
    path: str | os.PathLike[str],
    default: T,
    update: Callable[[T], Any],
) -> Any:
    """Atomically mutate JSON, replacing the document only when it changed."""
    resolved = Path(path)
    with file_lock(resolved):
        current = _read_json_unlocked(resolved, default)
        previous = deepcopy(current)
        result = update(current)
        if current != previous:
            _write_json_unlocked(resolved, current)
        return result
