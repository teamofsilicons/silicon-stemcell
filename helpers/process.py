"""Small bounded executor for best-effort runtime bookkeeping.

Primary messages must never wait for progress frames, read receipts, activity
logs, or durable work-card updates.  This module gives those side effects a
bounded in-process outbox with optional coalescing and per-key ordering.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class _Item:
    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    key: str
    coalesce: bool


class BestEffortOutbox:
    """Run ancillary calls without back-pressuring primary delivery.

    A key acts as an ordering lane.  Coalesced submissions replace an older
    queued item in the same lane (useful for progress and room invalidations).
    Running work is never interrupted; at most one successor is retained when
    coalescing is enabled.
    """

    def __init__(self, *, max_pending: int = 1_024, workers: int = 4):
        self.max_pending = max(1, int(max_pending))
        self._condition = threading.Condition()
        self._pending: deque[_Item] = deque()
        self._coalesced: dict[str, _Item] = {}
        self._running_keys: set[str] = set()
        self._active = 0
        self._closed = False
        self._threads: list[threading.Thread] = []
        for index in range(max(1, int(workers))):
            thread = threading.Thread(
                target=self._worker,
                name=f"best-effort-outbox-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def submit(
        self,
        function: Callable[..., Any],
        *args: Any,
        key: str = "",
        coalesce: bool = False,
        **kwargs: Any,
    ) -> bool:
        """Queue work immediately, returning False when the bound is reached."""
        lane = str(key or "")
        with self._condition:
            if self._closed:
                return False
            if coalesce and lane:
                existing = self._coalesced.get(lane)
                if existing is not None:
                    existing.function = function
                    existing.args = args
                    existing.kwargs = kwargs
                    return True
            if len(self._pending) >= self.max_pending:
                return False
            item = _Item(function, args, kwargs, lane, bool(coalesce))
            self._pending.append(item)
            if coalesce and lane:
                self._coalesced[lane] = item
            self._condition.notify()
            return True

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait for queued work. Intended for tests and graceful shutdown."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._pending or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, *, wait: bool = False, timeout: float = 5.0) -> None:
        if wait:
            self.flush(timeout)
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _next_item(self) -> _Item | None:
        for item in self._pending:
            if item.key and item.key in self._running_keys:
                continue
            self._pending.remove(item)
            if item.coalesce and item.key:
                self._coalesced.pop(item.key, None)
            if item.key:
                self._running_keys.add(item.key)
            self._active += 1
            return item
        return None

    def _worker(self) -> None:
        while True:
            with self._condition:
                item = self._next_item()
                while item is None:
                    if self._closed and not self._pending:
                        return
                    self._condition.wait()
                    item = self._next_item()
            try:
                item.function(*item.args, **item.kwargs)
            except Exception:
                # Every caller explicitly chose best-effort execution.
                pass
            finally:
                with self._condition:
                    self._active -= 1
                    if item.key:
                        self._running_keys.discard(item.key)
                    self._condition.notify_all()


OUTBOX = BestEffortOutbox()


def submit_best_effort(
    function: Callable[..., Any],
    *args: Any,
    key: str = "",
    coalesce: bool = False,
    **kwargs: Any,
) -> bool:
    return OUTBOX.submit(
        function,
        *args,
        key=key,
        coalesce=coalesce,
        **kwargs,
    )


def flush_best_effort(timeout: float = 5.0) -> bool:
    return OUTBOX.flush(timeout)
