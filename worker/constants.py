"""Where a worker's files live, and the one migration that put them there.

`worker/` holds code. What a worker produced is a log under ``logs/worker/``;
what Silicon knows about its workers is durable state under
``interface/state/workers/``. Both are reached through this module so nothing
imports a sibling just to learn a filename, and a test has one place to patch.
"""
from __future__ import annotations

import ast
import os
import platform
from pathlib import Path

from diagnostics.logs import LOGS_DIR
from helpers.migrate import copy_tree_once
from helpers.paths import CODE_ROOT, DATA_ROOT, STATE_DIR

IS_WINDOWS = platform.system() == "Windows"

CODE_WORKER_DIR = os.fspath(CODE_ROOT / "worker")
PROJECT_ROOT = os.fspath(DATA_ROOT)
WORKSPACE_ROOT = os.fspath(CODE_ROOT)
WORKER_DIR = os.path.join(PROJECT_ROOT, "worker")
SILICON_CONFIG_FILE = os.path.join(PROJECT_ROOT, "silicon.json")

LEGACY_OUTPUTS_DIR = os.path.join(WORKER_DIR, "outputs")
OUTPUTS_DIR = os.fspath(LOGS_DIR / "worker")
WORKER_STATE_DIR = os.fspath(STATE_DIR / "workers")
copy_tree_once(Path(LEGACY_OUTPUTS_DIR), Path(OUTPUTS_DIR))
os.makedirs(OUTPUTS_DIR, exist_ok=True)
os.makedirs(WORKER_STATE_DIR, exist_ok=True)

ACTIVE_FILE = os.path.join(WORKER_STATE_DIR, "_active_workers.json")
BROWSER_QUEUE_FILE = os.path.join(WORKER_STATE_DIR, "_browser_queue.json")
ARCHIVE_META_FILE = os.path.join(WORKER_STATE_DIR, "_archive_meta.json")
WORKER_REGISTRY_FILE = os.path.join(WORKER_STATE_DIR, "_worker_registry.json")
PROFILED_BROWSER_LOCK_FILE = os.path.join(
    WORKER_STATE_DIR,
    ".profiled-browser-launch.json",
)

BROWSER_WORKER_MODEL = "sonnet"
WORKER_PROVIDER_FALLBACKS = {
    "browser": ["claude"],
    "terminal": ["claude"],
    "writer": ["claude"],
}
VALID_WORKER_PROVIDERS = {"claude", "codex", "chatgpt"}
VALID_WORKER_TYPES = ("browser", "terminal", "writer")


def _legacy_browser_profile():
    """Read the one supported legacy env.py value without executing the file."""

    path = DATA_ROOT / "env.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return "silicon"
    for node in tree.body:
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(getattr(node, "value", None), ast.Constant)
            and isinstance(node.value.value, str)
        ):
            names = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
            if any(
                isinstance(name, ast.Name) and name.id == "BROWSER_PROFILE"
                for name in names
            ):
                return node.value.value
    return "silicon"


_BROWSER_PROFILE = _legacy_browser_profile()
SILICON_BROWSER_PROFILE = _BROWSER_PROFILE
