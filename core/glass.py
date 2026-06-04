"""Glass configuration helpers.

Messaging, media, crons, take-back, and remote browser events now move through
Silicon Interface. This module intentionally keeps only Glass config loading
for sidecar and backup code that needs direct Glass HTTP access.
"""
from __future__ import annotations

import json
from pathlib import Path

CONFIG_FILE = ".glass.json"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def find_glass_config(start: str | Path | None = None) -> Path | None:
    current = Path(start or PROJECT_ROOT).resolve()
    for candidate in [current, *current.parents]:
        path = candidate / CONFIG_FILE
        if path.exists():
            return path
    return None


def load_glass_config(start: str | Path | None = None) -> tuple[dict, Path]:
    path = find_glass_config(start)
    if path is None:
        raise FileNotFoundError("No .glass.json found in this folder or its parents.")
    return json.loads(path.read_text(encoding="utf-8")), path


def auth_headers(config: dict) -> dict[str, str]:
    key = config.get("api_key") or config.get("silicon_api_key") or ""
    return {"Authorization": f"Bearer {key}"} if key else {}
