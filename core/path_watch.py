"""Portable file-change waits with native notifications and a polling fallback."""
from __future__ import annotations

import ctypes
import os
import select
import struct
import sys
import threading
import time
from pathlib import Path
from collections.abc import Iterable
from typing import Protocol


class _NativeBackend(Protocol):
    def wait(self, timeout: float) -> bool: ...

    def close(self) -> None: ...


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def _path_set(value: Iterable[str | os.PathLike[str]]) -> tuple[Path, ...]:
    paths = {
        Path(path).expanduser().resolve()
        for path in value
    }
    if not paths:
        raise ValueError("At least one path is required.")
    return tuple(sorted(paths, key=os.fspath))


class _InotifyBackend:
    _EVENT = struct.Struct("iIII")
    _IN_ATTRIB = 0x00000004
    _IN_CLOSE_WRITE = 0x00000008
    _IN_MOVED_TO = 0x00000080
    _IN_CREATE = 0x00000100
    _IN_DELETE = 0x00000200
    _IN_DELETE_SELF = 0x00000400
    _IN_MOVE_SELF = 0x00000800
    _IN_Q_OVERFLOW = 0x00004000

    def __init__(self, path: Path):
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        add_watch = libc.inotify_add_watch
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int

        fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        mask = (
            self._IN_ATTRIB
            | self._IN_CLOSE_WRITE
            | self._IN_MOVED_TO
            | self._IN_CREATE
            | self._IN_DELETE
            | self._IN_DELETE_SELF
            | self._IN_MOVE_SELF
        )
        watch = add_watch(fd, os.fsencode(path.parent), mask)
        if watch < 0:
            error = ctypes.get_errno()
            os.close(fd)
            raise OSError(error, os.strerror(error))
        self._fd = fd
        self._watch = watch
        self._filename = os.fsencode(path.name)

    def wait(self, timeout: float) -> bool:
        readable, _, _ = select.select(
            [self._fd],
            [],
            [],
            max(0.0, float(timeout)),
        )
        if not readable:
            return False
        try:
            payload = os.read(self._fd, 64 * 1024)
        except BlockingIOError:
            return False
        offset = 0
        relevant = False
        while offset + self._EVENT.size <= len(payload):
            watch, mask, _cookie, name_length = self._EVENT.unpack_from(
                payload,
                offset,
            )
            offset += self._EVENT.size
            raw_name = payload[offset : offset + name_length]
            offset += name_length
            name = raw_name.split(b"\0", 1)[0]
            if (
                mask & self._IN_Q_OVERFLOW
                or watch == self._watch
                and (not name or name == self._filename)
            ):
                relevant = True
        return relevant

    def close(self) -> None:
        fd = getattr(self, "_fd", -1)
        self._fd = -1
        if fd >= 0:
            os.close(fd)


class _InotifySetBackend:
    """One inotify descriptor covering target files across several folders."""

    _EVENT = _InotifyBackend._EVENT
    _MASK = (
        _InotifyBackend._IN_ATTRIB
        | _InotifyBackend._IN_CLOSE_WRITE
        | _InotifyBackend._IN_MOVED_TO
        | _InotifyBackend._IN_CREATE
        | _InotifyBackend._IN_DELETE
        | _InotifyBackend._IN_DELETE_SELF
        | _InotifyBackend._IN_MOVE_SELF
    )
    _IN_Q_OVERFLOW = _InotifyBackend._IN_Q_OVERFLOW

    def __init__(self, paths: tuple[Path, ...]):
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        add_watch = libc.inotify_add_watch
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int

        fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        self._fd = fd
        self._targets: dict[int, set[bytes]] = {}
        try:
            by_parent: dict[Path, set[bytes]] = {}
            for path in paths:
                by_parent.setdefault(path.parent, set()).add(
                    os.fsencode(path.name)
                )
            for parent, names in by_parent.items():
                watch = add_watch(fd, os.fsencode(parent), self._MASK)
                if watch < 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error))
                self._targets.setdefault(watch, set()).update(names)
        except Exception:
            self.close()
            raise

    def wait(self, timeout: float) -> bool:
        readable, _, _ = select.select(
            [self._fd],
            [],
            [],
            max(0.0, float(timeout)),
        )
        if not readable:
            return False
        try:
            payload = os.read(self._fd, 64 * 1024)
        except BlockingIOError:
            return False
        offset = 0
        while offset + self._EVENT.size <= len(payload):
            watch, mask, _cookie, name_length = self._EVENT.unpack_from(
                payload,
                offset,
            )
            offset += self._EVENT.size
            raw_name = payload[offset : offset + name_length]
            offset += name_length
            name = raw_name.split(b"\0", 1)[0]
            if mask & self._IN_Q_OVERFLOW:
                return True
            targets = self._targets.get(watch, set())
            if not name or name in targets:
                return True
        return False

    def close(self) -> None:
        fd = getattr(self, "_fd", -1)
        self._fd = -1
        self._targets = {}
        if fd >= 0:
            os.close(fd)


class _KqueueBackend:
    def __init__(self, path: Path):
        self._fds = [os.open(path.parent, os.O_RDONLY)]
        self._queue = None
        try:
            self._queue = select.kqueue()
            flags = (
                select.KQ_NOTE_WRITE
                | select.KQ_NOTE_EXTEND
                | select.KQ_NOTE_ATTRIB
                | select.KQ_NOTE_LINK
                | select.KQ_NOTE_RENAME
                | select.KQ_NOTE_DELETE
            )
            try:
                self._fds.append(os.open(path, os.O_RDONLY))
            except OSError:
                pass
            changes = [
                select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=flags,
                )
                for fd in self._fds
            ]
            self._queue.control(changes, 0, 0)
        except Exception:
            self.close()
            raise

    def wait(self, timeout: float) -> bool:
        if self._queue is None:
            return False
        return bool(
            self._queue.control(
                [],
                1,
                max(0.0, float(timeout)),
            )
        )

    def close(self) -> None:
        queue = getattr(self, "_queue", None)
        self._queue = None
        if queue is not None:
            queue.close()
        fds = getattr(self, "_fds", [])
        self._fds = []
        for fd in fds:
            if fd >= 0:
                os.close(fd)


def _create_native_backend(path: Path) -> _NativeBackend | None:
    try:
        if sys.platform.startswith("linux"):
            return _InotifyBackend(path)
        if hasattr(select, "kqueue"):
            return _KqueueBackend(path)
    except (AttributeError, OSError):
        return None
    return None


def _create_native_set_backend(
    paths: tuple[Path, ...],
) -> _NativeBackend | None:
    try:
        if sys.platform.startswith("linux"):
            return _InotifySetBackend(paths)
        if hasattr(select, "kqueue"):
            # Kqueue has no inotify-style filename filter. Watching the target
            # files and their parent folders is still cheap; signatures below
            # discard unrelated directory activity.
            return _KqueueSetBackend(paths)
    except (AttributeError, OSError):
        return None
    return None


class _KqueueSetBackend:
    def __init__(self, paths: tuple[Path, ...]):
        self._fds: list[int] = []
        self._queue = None
        try:
            self._queue = select.kqueue()
            opened: set[Path] = set()
            for path in paths:
                for target in (path.parent, path):
                    if target in opened:
                        continue
                    try:
                        self._fds.append(os.open(target, os.O_RDONLY))
                        opened.add(target)
                    except OSError:
                        pass
            flags = (
                select.KQ_NOTE_WRITE
                | select.KQ_NOTE_EXTEND
                | select.KQ_NOTE_ATTRIB
                | select.KQ_NOTE_LINK
                | select.KQ_NOTE_RENAME
                | select.KQ_NOTE_DELETE
            )
            changes = [
                select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=flags,
                )
                for fd in self._fds
            ]
            self._queue.control(changes, 0, 0)
        except Exception:
            self.close()
            raise

    def wait(self, timeout: float) -> bool:
        if self._queue is None:
            return False
        return bool(
            self._queue.control(
                [],
                1,
                max(0.0, float(timeout)),
            )
        )

    def close(self) -> None:
        queue = getattr(self, "_queue", None)
        self._queue = None
        if queue is not None:
            queue.close()
        fds = getattr(self, "_fds", [])
        self._fds = []
        for fd in fds:
            if fd >= 0:
                os.close(fd)


class PathChangeWaiter:
    """Wait for one path to change without making native support mandatory."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        fallback_poll_seconds: float = 0.1,
    ):
        self.path = Path(path).expanduser().resolve()
        self.fallback_poll_seconds = max(
            0.01,
            float(fallback_poll_seconds),
        )
        self._last_signature = _signature(self.path)
        self._native = _create_native_backend(self.path)

    @property
    def native_notifications(self) -> bool:
        return self._native is not None

    def _close_native(self) -> None:
        native = self._native
        self._native = None
        if native is not None:
            try:
                native.close()
            except OSError:
                pass

    def _changed(self) -> bool:
        current = _signature(self.path)
        if current == self._last_signature:
            return False
        self._last_signature = current
        return True

    def wait(
        self,
        timeout: float,
        stop_event: threading.Event | None = None,
    ) -> bool:
        """Return true when the target changes, or false on timeout/stop."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while stop_event is None or not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._changed()

            if self._native is None:
                self._native = _create_native_backend(self.path)
            native = self._native
            interval = min(remaining, 0.5 if stop_event is not None else remaining)
            if native is not None:
                try:
                    if native.wait(interval) and self._changed():
                        # Atomic replacement invalidates target-inode watches.
                        # Re-arming both backends also drains stale event state.
                        self._close_native()
                        return True
                except (OSError, ValueError):
                    self._close_native()
                continue

            interval = min(interval, self.fallback_poll_seconds)
            if stop_event is not None:
                stop_event.wait(interval)
            else:
                time.sleep(interval)
            if self._changed():
                return True
        return False

    def close(self) -> None:
        self._close_native()

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class PathSetChangeWaiter:
    """Wait for any of several paths using one native notification backend."""

    def __init__(
        self,
        paths: Iterable[str | os.PathLike[str]],
        *,
        fallback_poll_seconds: float = 0.1,
    ):
        self.paths = _path_set(paths)
        self.fallback_poll_seconds = max(
            0.01,
            float(fallback_poll_seconds),
        )
        self._last_signatures = {
            path: _signature(path)
            for path in self.paths
        }
        self._native = _create_native_set_backend(self.paths)

    @property
    def native_notifications(self) -> bool:
        return self._native is not None

    def _close_native(self) -> None:
        native = self._native
        self._native = None
        if native is not None:
            try:
                native.close()
            except OSError:
                pass

    def _changed(self) -> bool:
        changed = False
        for path in self.paths:
            current = _signature(path)
            if current != self._last_signatures[path]:
                self._last_signatures[path] = current
                changed = True
        return changed

    def wait(
        self,
        timeout: float,
        stop_event: threading.Event | None = None,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while stop_event is None or not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._changed()

            if self._native is None:
                self._native = _create_native_set_backend(self.paths)
            native = self._native
            interval = min(
                remaining,
                0.5 if stop_event is not None else remaining,
            )
            if native is not None:
                try:
                    if native.wait(interval) and self._changed():
                        self._close_native()
                        return True
                except (OSError, ValueError):
                    self._close_native()
                continue

            interval = min(interval, self.fallback_poll_seconds)
            if stop_event is not None:
                stop_event.wait(interval)
            else:
                time.sleep(interval)
            if self._changed():
                return True
        return False

    def close(self) -> None:
        self._close_native()

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
