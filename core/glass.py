"""Glass configuration helpers.

Messaging, media, crons, take-back, and remote browser events now move through
Silicon Interface. This module intentionally keeps only Glass config loading
for sidecar and backup code that needs direct Glass HTTP access.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

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


def server_and_key(config: dict | None = None) -> tuple[str, str]:
    """The Glass base URL and this silicon's API key, from .glass.json."""
    if config is None:
        config, _ = load_glass_config()
    server = (config.get("server_url") or "").rstrip("/")
    key = config.get("api_key") or config.get("silicon_api_key") or ""
    return server, key


def silicon_api_post(path: str, json_body: dict | None = None, timeout: int = 15):
    """POST to the Glass API authenticated as this silicon (X-Silicon-Key).

    Same auth the interface CLI uses, so the stemcell can hit silicon-only
    endpoints directly without shelling out to the CLI. Raises on failure.
    """
    server, key = server_and_key()
    if not server or not key:
        raise RuntimeError("Glass server_url/api_key not configured in .glass.json")
    resp = requests.post(
        f"{server}{path}",
        headers={"X-Silicon-Key": key},
        json=json_body or {},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp


def load_provider_keys_into_env(config: dict | None = None) -> dict[str, str]:
    """Fetch this silicon's provider API keys from Glass and export them to env.

    Glass is the single source of truth for provider secrets; nothing is stored
    locally. The brain CLIs (claude/codex) and the browser tool (silicon-browser)
    run as subprocesses that inherit ``os.environ``, so exporting the keys here —
    once, before anything else runs — makes them available everywhere.

    Best-effort: any failure is logged and returns ``{}`` so the silicon still
    boots (tools needing a missing key will report it themselves).
    """
    try:
        if config is None:
            config, _ = load_glass_config()
        server = (config.get("server_url") or "").rstrip("/")
        key = config.get("api_key") or config.get("silicon_api_key") or ""
        if not server or not key:
            return {}
        resp = requests.get(
            f"{server}/api/v1/silicons/me/provider-keys",
            headers={"X-Silicon-Key": key},
            timeout=15,
        )
        resp.raise_for_status()
        keys = (resp.json() or {}).get("keys") or {}
        applied: dict[str, str] = {}
        for name, value in keys.items():
            if isinstance(name, str) and isinstance(value, str) and value:
                os.environ[name] = value
                applied[name] = value
        return applied
    except Exception as exc:  # noqa: BLE001 — boot must not fail on key fetch
        print(f"[silicon] could not load provider keys from Glass: {exc}", flush=True)
        return {}
