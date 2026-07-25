"""Seed per-Silicon living files from tracked templates.

Update ownership belongs to ``silicon-cli``.  Runtime startup only creates
missing identity/memory files; it never fetches or mutates source control.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from core.runtime_paths import CODE_ROOT, DATA_ROOT

PROJECT_ROOT = DATA_ROOT
TEMPLATES_DIR = CODE_ROOT / "templates"


def seed_living_files(
    root: str | os.PathLike | None = None,
    templates: str | os.PathLike | None = None,
) -> list[str]:
    """Copy template files that do not yet exist, never overwriting living data."""

    target_root = Path(root).resolve() if root is not None else DATA_ROOT
    template_root = (
        Path(templates).resolve()
        if templates is not None
        else TEMPLATES_DIR
    )
    if not template_root.exists():
        return []

    seeded: list[str] = []
    for current, _directories, files in os.walk(template_root):
        for filename in files:
            source = Path(current) / filename
            relative = source.relative_to(template_root)
            destination = target_root / relative
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            seeded.append(relative.as_posix())
    return seeded
