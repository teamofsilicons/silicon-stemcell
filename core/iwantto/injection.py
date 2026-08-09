"""Putting a new message into a run that is already going.

A manager mid-turn used to be unreachable: a message arriving while it worked
waited for the whole run to finish before anything saw it. Both providers can
actually take input during a turn — Claude through streaming stdin, Codex
through `turn/steer` — and the model picks it up at the next tool boundary and
adapts, rather than being interrupted.

This is the registry that connects the two: a live run registers how to reach
it, and whoever holds a new message offers it here first. An offer that is
refused — no live run, or the run has stopped accepting — falls back to the
durable path it would have taken anyway, so nothing is ever lost by trying.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

MANAGER = "manager"
WORKER = "worker"

_LOCK = threading.RLock()
_LIVE: dict[tuple, object] = {}


def _key(kind: str, actor_id: str) -> tuple:
    return (str(kind or ""), str(actor_id or ""))


@contextmanager
def accepting(kind: str, actor_id: str, submit):
    """Mark a run as reachable for the duration of the block.

    ``submit(text) -> bool`` must be safe to call from another thread and must
    return False rather than raise once it can no longer deliver.
    """
    key = _key(kind, actor_id)
    with _LOCK:
        previous = _LIVE.get(key)
        _LIVE[key] = submit
    try:
        yield
    finally:
        with _LOCK:
            # Only clear our own registration: a retry may already have
            # replaced it with a newer run for the same actor.
            if _LIVE.get(key) is submit:
                if previous is None:
                    _LIVE.pop(key, None)
                else:
                    _LIVE[key] = previous


def is_live(kind: str, actor_id: str) -> bool:
    with _LOCK:
        return _key(kind, actor_id) in _LIVE


def offer(kind: str, actor_id: str, text: str) -> bool:
    """Try to push text into a live run. False means "use the durable path"."""
    if not text:
        return False
    with _LOCK:
        submit = _LIVE.get(_key(kind, actor_id))
    if submit is None:
        return False
    try:
        return bool(submit(text))
    except Exception:
        # A failing injector must never take down the caller's own delivery.
        return False


def live_actors() -> list:
    with _LOCK:
        return sorted(_LIVE)
