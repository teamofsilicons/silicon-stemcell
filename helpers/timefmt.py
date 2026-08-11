"""Time, in the two shapes Silicon writes down."""
from __future__ import annotations

import datetime
import time


def now() -> float:
    """Wall-clock seconds. Monotonic time is for deadlines, not for records."""
    return time.time()


def utc_iso(ts: float | None = None) -> str:
    """An instant as `YYYY-MM-DDTHH:MM:SS+00:00`, for anything durable."""
    moment = datetime.datetime.fromtimestamp(
        now() if ts is None else ts, datetime.timezone.utc
    )
    return moment.isoformat()
