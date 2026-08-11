"""Where a conversation's continuity is kept between turns.

One file per (session key, provider) under ``sessions/``. Claude stores a UUID
it is told to resume; Codex stores a thread id the server hands back. Deleting
the file is how a session is reset — the next turn starts a fresh one.
"""
from __future__ import annotations

import os
import uuid

from helpers.paths import DATA_ROOT

SESSIONS_DIR = os.path.join(os.fspath(DATA_ROOT), "sessions")


class SessionStore:
    """The on-disk session ids for one provider."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def path(self, session_key: str) -> str:
        # Claude's files are unsuffixed: they predate multi-provider support and
        # renaming them would orphan every live manager session.
        suffix = "" if self.provider == "claude" else f"_{self.provider}"
        return os.path.join(SESSIONS_DIR, f"{session_key}{suffix}.txt")

    def read(self, session_key: str) -> str:
        path = self.path(session_key)
        if not os.path.exists(path):
            return ""
        try:
            with open(path) as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def write(self, session_key: str, session_id: str) -> str:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        with open(self.path(session_key), "w") as handle:
            handle.write(session_id)
        return session_id

    def clear(self, session_key: str) -> None:
        path = self.path(session_key)
        if os.path.exists(path):
            os.remove(path)

    def new_uuid(self, session_key: str) -> str:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        return self.write(session_key, str(uuid.uuid4()))

    def read_or_create_uuid(self, session_key: str) -> str:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        return self.read(session_key) or self.new_uuid(session_key)


def prompt_file(session_key: str, prompt: str) -> str:
    """Write a system prompt where the provider CLI can read it back."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = os.path.join(SESSIONS_DIR, f"{session_key}_prompt.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(prompt)
    return path
