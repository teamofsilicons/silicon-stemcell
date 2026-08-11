"""One-time moves of on-disk layout, done by copying.

Renaming a directory that a live Silicon is using loses data if anything goes
wrong halfway. These migrations copy instead: the old path is left exactly as
it was, a marker records that the copy happened, and a second run does nothing.
Deleting the leftovers is a decision for whoever is watching, not for boot.
"""
from __future__ import annotations

import shutil
from pathlib import Path

MARKER = ".migrated"


def copy_tree_once(legacy: Path, current: Path) -> bool:
    """Copy ``legacy`` to ``current`` the first time, and never again.

    Returns True when this call did the copy.
    """
    if current.exists() or not legacy.is_dir():
        return False
    marker = legacy / MARKER
    if marker.exists():
        return False
    try:
        current.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy, current)
        marker.write_text(f"copied to {current}\n", encoding="utf-8")
    except OSError:
        # A failed migration must not stop a Silicon from starting. The legacy
        # directory is untouched, so the next boot tries again.
        return False
    return True


def copy_file_once(legacy: Path, current: Path) -> bool:
    """Copy one file to its new name if the new name is not there yet."""
    if current.exists() or not legacy.is_file():
        return False
    try:
        current.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, current)
    except OSError:
        return False
    return True
