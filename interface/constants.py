"""Where Interface state lives, and the shapes the wire uses.

One place for the paths, event vocabulary, and timing constants the Interface
modules share, so none of them has to import another just to learn a filename.
"""
from __future__ import annotations

import os
import re

from helpers.paths import DATA_ROOT, STATE_DIR

PROJECT_ROOT = DATA_ROOT
CONTACTS_FILE = STATE_DIR / "contacts.json"
CONTACTS_BACKUP_FILE = STATE_DIR / "contacts_backup.json"
MEDIA_DIR = STATE_DIR / "media"
INBOX_CONSUMER_FILE = STATE_DIR / "interface_inbox_consumer.json"
REMOTE_BROWSER_STATE_FILE = STATE_DIR / "remote_browser.json"
DEFAULT_INBOX_FILE = PROJECT_ROOT / ".silicon-interface" / "inbox.jsonl"

VALID_TRUST_LEVELS = ["very_low", "low", "ok", "high", "very_high", "ultimate"]
USER_VISIBLE_EVENT_TYPES = {"m.text", "m.image", "m.file", "m.album", "m.voice", "m.tts"}
IGNORED_EVENT_TYPES = {"m.progress", "m.reaction", "m.session_marker", "m.system"}
RICH_MEDIA_RE = re.compile(r"\[(file|voice)=((?:[^\[\]]|\[[^\]]*\])*)\]", re.DOTALL)
URL_RE = re.compile(r"https?://[^\s\"\'<>]+")
REMOTE_BROWSER_START_URL = os.environ.get(
    "SILICON_REMOTE_BROWSER_START_URL", "https://www.google.com"
)

INBOX_POLL_SECONDS = 0.1
RUNTIME_FILE_POLL_SECONDS = 0.5
DAEMON_HEALTH_SECONDS = 15
DAEMON_DEEP_HEALTH_SECONDS = 5 * 60
DAEMON_DEEP_HEALTH_JITTER_SECONDS = 60
ROOM_SYNC_FALLBACK_SECONDS = 15 * 60
INBOX_READ_CHUNK_BYTES = 4 * 1024 * 1024
RPC_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
