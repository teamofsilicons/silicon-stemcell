"""Following this Silicon's own log, and doing work only when something changed.

The tailer streams new runtime log lines to the console without re-reading the
file, and survives rotation. The worker beside it runs a job when a watched
path changes, and otherwise sleeps — which is what keeps an idle sidecar idle.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from helpers.timefmt import utc_iso as _utc_iso

from helpers.watch import PathSetChangeWaiter

RUNTIME_LOG_INITIAL_LINES = 10
RUNTIME_LOG_BATCH_LINES = 100
RUNTIME_LOG_MAX_LINE_BYTES = 16 * 1024
RUNTIME_LOG_INITIAL_SCAN_BYTES = 256 * 1024
RUNTIME_LOG_ANCHOR_BYTES = 64


def runtime_log_level(line: str) -> str:
    """Infer a useful Glass display level without changing the log text."""

    lowered = line.lower()
    if any(
        marker in lowered
        for marker in ("error", "exception", "traceback", "fatal", "failed")
    ):
        return "error"
    if "warning" in lowered or "warn" in lowered:
        return "warn"
    return "info"


class RuntimeLogTailer:
    """Incrementally mirror the same process log shown by ``silicon debug``.

    The cursor lives for the lifetime of the Glass sidecar, rather than for one
    WebSocket, so a reconnect catches up without replaying already-sent lines.
    File identity, size, and a short byte anchor make normal replacement and
    copy-truncate rotation safe.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._identity: tuple[int, int] | None = None
        self._position: int | None = None
        self._anchor = b""

    @staticmethod
    def _file_identity(metadata) -> tuple[int, int]:
        return int(metadata.st_dev), int(metadata.st_ino)

    @staticmethod
    def _read_anchor(handle, position: int) -> bytes:
        length = min(max(0, position), RUNTIME_LOG_ANCHOR_BYTES)
        if not length:
            return b""
        handle.seek(position - length)
        return handle.read(length)

    @staticmethod
    def _initial_position(handle, size: int) -> int:
        """Match ``tail -f`` by starting with at most ten recent lines."""

        start = max(0, size - RUNTIME_LOG_INITIAL_SCAN_BYTES)
        handle.seek(start)
        sample = handle.read(size - start)
        if start:
            newline = sample.find(b"\n")
            if newline < 0:
                return size
            start += newline + 1
            sample = sample[newline + 1 :]
        lines = sample.splitlines(keepends=True)
        return start + len(sample) - sum(
            len(line) for line in lines[-RUNTIME_LOG_INITIAL_LINES:]
        )

    def _prepare(self, handle) -> None:
        metadata = os.fstat(handle.fileno())
        identity = self._file_identity(metadata)
        size = int(metadata.st_size)

        if self._identity != identity:
            self._identity = identity
            self._position = (
                self._initial_position(handle, size)
                if self._position is None
                else 0
            )
            self._anchor = self._read_anchor(handle, self._position)
            return

        position = int(self._position or 0)
        replaced = size < position
        if not replaced and self._anchor:
            replaced = self._read_anchor(handle, position) != self._anchor
        if replaced:
            self._position = 0
            self._anchor = b""

    def poll(self, send) -> int:
        """Send newly completed log lines as bounded Glass log frames."""

        try:
            handle = open(self.path, "rb")
        except (FileNotFoundError, IsADirectoryError, OSError):
            return 0

        sent = 0
        with handle:
            self._prepare(handle)
            handle.seek(int(self._position or 0))
            while sent < RUNTIME_LOG_BATCH_LINES:
                raw = handle.readline(RUNTIME_LOG_MAX_LINE_BYTES + 1)
                if not raw:
                    break

                complete = raw.endswith((b"\n", b"\r"))
                if not complete and len(raw) <= RUNTIME_LOG_MAX_LINE_BYTES:
                    # Do not show a process write until its line is complete.
                    handle.seek(int(self._position or 0))
                    break

                omitted = 0
                if len(raw) > RUNTIME_LOG_MAX_LINE_BYTES:
                    kept = raw[:RUNTIME_LOG_MAX_LINE_BYTES]
                    omitted = len(raw) - len(kept)
                    raw = kept
                    while not complete:
                        remainder = handle.readline(RUNTIME_LOG_MAX_LINE_BYTES)
                        if not remainder:
                            break
                        omitted += len(remainder)
                        complete = remainder.endswith((b"\n", b"\r"))

                line = raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
                if omitted:
                    line += f" …(+{omitted} bytes truncated)"
                frame = {
                    "type": "log",
                    "level": runtime_log_level(line),
                    "source": "silicon",
                    "ts": _utc_iso(),
                    "msg": line,
                }
                send(frame)
                self._position = handle.tell()
                self._anchor = self._read_anchor(handle, self._position)
                handle.seek(self._position)
                sent += 1
        return sent


class ChangeDrivenWorker:
    """Run one sidecar callback on file changes with a slow safety fallback."""

    def __init__(
        self,
        paths: list[Path],
        callback,
        *,
        fallback_seconds: float,
        polling_seconds: float,
        name: str,
        on_error=None,
    ):
        self.paths = [Path(path) for path in paths]
        self.callback = callback
        self.fallback_seconds = max(0.1, float(fallback_seconds))
        self.polling_seconds = max(0.05, float(polling_seconds))
        self.name = name
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=self.name,
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            with PathSetChangeWaiter(
                self.paths,
                fallback_poll_seconds=self.polling_seconds,
            ) as changes:
                while not self._stop.is_set():
                    changes.wait(self.fallback_seconds, self._stop)
                    if self._stop.is_set():
                        return
                    try:
                        self.callback()
                    except Exception as exc:
                        if self.on_error is not None:
                            self.on_error(exc)
                        return
        except Exception as exc:
            if self.on_error is not None:
                self.on_error(exc)

    def stop(self, timeout: float = 2) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)


