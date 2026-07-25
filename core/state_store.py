"""Cross-thread/process locking and atomic JSON persistence."""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
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


def _lock_file(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


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
            _lock_file(handle)
            depths[key] = 1
            yield
        finally:
            depths.pop(key, None)
            try:
                _unlock_file(handle)
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
