"""Values the manager runtime shares, in one place.

Retry budgets, the two replies a timed-out turn produces, the restart flags,
and the progress vocabulary a provider event is mapped onto.
"""
from __future__ import annotations

import os

from helpers.paths import DATA_ROOT

PROJECT_ROOT = os.fspath(DATA_ROOT)

# One fresh-thread retry bounds a silent provider failure to two inactivity
# windows. A second timeout pauses the durable task and releases the contact
# dispatcher instead of occupying it for the remaining manager iterations.
MAX_MANAGER_TIMEOUT_RETRIES = 1
MANAGER_TIMEOUT_RETRY_REPLY = (
    "The manager stopped responding before it produced a result. "
    "I’m retrying once with a fresh session."
)
MANAGER_TIMEOUT_FINAL_REPLY = (
    "I couldn’t complete this request because the manager provider stopped "
    "responding twice. The task is paused; send a new message to resume it."
)

RESTART_FLAG = os.path.join(PROJECT_ROOT, ".restart_pending")

RESTART_REQUEST_FILE = os.path.join(PROJECT_ROOT, ".restart_requested")


PROVIDER_PROGRESS_STATES = {
    "reading_file",
    "writing_file",
    "executing",
    "searching_web",
    "thinking",
}

_TERMINAL_BRAIN_FAILURE_MARKERS = ("usage limit", "not authenticated")
