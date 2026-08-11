"""How long a turn may take, and whether it stays reachable while it runs.

A turn has both an absolute ceiling and an inactivity ceiling. The second one
catches app-server turns that report thinking as finished and then never emit
text, a tool, an error, or ``turn/completed``. Long work belongs in a worker
rather than holding a contact's serialized manager queue.
"""
from __future__ import annotations

import os

TURN_TIMEOUT = max(
    60.0,
    float(os.environ.get("SILICON_MANAGER_TIMEOUT_SECONDS", str(30 * 60))),
)
INACTIVITY_TIMEOUT = max(
    30.0,
    min(
        TURN_TIMEOUT,
        float(os.environ.get("SILICON_MANAGER_INACTIVITY_SECONDS", "180")),
    ),
)

# Streaming stdin is what makes a manager reachable mid-turn. Set
# SILICON_STREAMING_INPUT=0 to fall back to the single-shot behaviour.
STREAMING_INPUT = os.environ.get("SILICON_STREAMING_INPUT", "1") != "0"
