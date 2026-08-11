"""The one lock that serializes everything this package does.

A thread lock per path and a cross-process advisory lock over the same file.
Both live here and nowhere else: a second copy of the table would give two
in-process locks for one path, and let a reconcile and a publish run at once
behind the same file lock.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import paths as paths_module
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from helpers.state import (
    lock_handle,
    unlock_handle,
)


_THREAD_LOCKS: dict[str, threading.Lock] = {}


_THREAD_LOCKS_GUARD = threading.Lock()


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
    path = paths_module._lock_file(root)
    paths_module._assert_local_path(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    before: os.stat_result | None = None
    if os.path.lexists(path):
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise errors_module.TeamContextError("Team context lock must be a local regular file.")
    local_lock = _thread_lock(path)
    if not local_lock.acquire(timeout=constants.LOCK_TIMEOUT_SECONDS):
        raise errors_module.TeamContextLockTimeout("Team context synchronization is already running.")

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
            raise errors_module.TeamContextError(
                "Team context lock changed while it was being opened."
            )
        paths_module._assert_local_path(root, path)
        deadline = time.monotonic() + constants.LOCK_TIMEOUT_SECONDS
        while not lock_handle(handle, blocking=False):
            if time.monotonic() >= deadline:
                raise errors_module.TeamContextLockTimeout(
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
