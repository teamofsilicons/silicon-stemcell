"""Paths, limits and patterns the team package shares.

Every name here is read as ``constants.X`` at call time, never imported by
name, so a test that swaps the data root or a limit has one seam.
"""
from __future__ import annotations

import hashlib
import re
from helpers.paths import DATA_ROOT


PROJECT_ROOT = DATA_ROOT


TEAM_CONTEXT_PATH = "prompts/TEAM.md"


ADVERTISING_DIRECTORY = "prompts/advertising"


STATE_PATH = "interface/state/team_context.json"


LOCK_PATH = "interface/state/team_context.lock"


DRAFT_ARCHIVE_DIRECTORY = "interface/state/team_context_drafts"


VISIBILITY_BLOCK_PATH = "interface/state/team_context.blocked"


MAX_ADVERTISING_MEMORY_LINES = 100


MAX_ADVERTISING_MEMORY_BYTES = 64 * 1024


MAX_ADVERTISED_MEMORY_LINES = 600


MAX_ADVERTISED_MEMORY_BYTES = 256 * 1024


MAX_TEAM_CONTEXT_BYTES = 256 * 1024


RECONCILE_INTERVAL_SECONDS = 60


LOCK_TIMEOUT_SECONDS = 10


MAX_PARALLEL_PEER_SYNCS = 4


TEAM_PLACEHOLDER_MARKDOWN = """# Silicon Team

_Team context has not been fetched from Glass yet._

Glass replaces this placeholder with the current team hierarchy and each
Silicon's name, description, job description, and advertising-memory path.
Advertising-memory contents are never embedded in this file.
"""


_STATE_VERSION = 1


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


_SILICON_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


_TEAM_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$")
