"""Values that cross the Interface boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboxRecord:
    """One complete durable CLI inbox line and its commit boundary."""

    frame: dict[str, Any]
    path: str = ""
    file_id: str = ""
    end_offset: int = 0

