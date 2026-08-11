"""Reaching a Claude turn that is already running."""
from __future__ import annotations

import threading

from inference.parsing import stream_json_user


class ClaudeInjector:
    """Writes a new user message into a running ``claude -p`` session.

    The session stays reachable until its first ``result``. After that the model
    has finished and anything newer belongs to the next run, so the injector
    refuses rather than writing into a turn that is already closing.
    """

    def __init__(self, proc, tag: str) -> None:
        self._proc = proc
        self._tag = tag
        self._lock = threading.Lock()
        self._open = True
        self.delivered = 0

    def submit(self, text) -> bool:
        with self._lock:
            if not self._open:
                return False
            try:
                self._proc.stdin.write(stream_json_user(text))
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._open = False
                return False
            self.delivered += 1
            print(f"  [{self._tag}] injected a new message mid-run", flush=True)
            return True

    def close(self) -> None:
        """Stop accepting and let the provider finish.

        Anything already written is in the pipe and will still be read, so a
        message accepted a moment before this is not lost.
        """
        with self._lock:
            if not self._open:
                return
            self._open = False
            try:
                self._proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
