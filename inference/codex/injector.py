"""Reaching a Codex turn that is already running."""
from __future__ import annotations

import threading


class CodexInjector:
    """Steers a live Codex turn with ``turn/steer``.

    Uses ``send`` rather than ``request``: ``request`` drains the shared message
    queue, which would steal events from the loop reading the turn. The response
    comes back through that loop instead.
    """

    def __init__(self, client, thread_id: str, turn_id: str, tag: str) -> None:
        self._client = client
        self._thread_id = thread_id
        self._turn_id = turn_id
        self._tag = tag
        self._lock = threading.Lock()
        self._open = bool(thread_id and turn_id)
        self.delivered = 0
        self.request_ids = set()

    def submit(self, text) -> bool:
        with self._lock:
            if not self._open:
                return False
            try:
                request_id = self._client.send(
                    "turn/steer",
                    {
                        "threadId": self._thread_id,
                        "expectedTurnId": self._turn_id,
                        "input": [{"type": "text", "text": str(text or "")}],
                    },
                )
            except (BrokenPipeError, OSError, ValueError):
                self._open = False
                return False
            self.request_ids.add(request_id)
            self.delivered += 1
            print(
                f"  [{self._tag}] steered the live turn with a new message",
                flush=True,
            )
            return True

    def close(self) -> None:
        with self._lock:
            self._open = False
