"""Manifest-based backups for a Silicon instance.

The instance root can contain a .backupsilicon manifest with one path or glob
per line. A Glass backup request archives those paths and uploads the result to
the silicon-backups API using the silicon API key in .glass.json.
"""
from __future__ import annotations

import glob
import io
import json
import os
import tarfile
from pathlib import Path

import requests

MANIFEST_NAME = ".backupsilicon"
UPLOAD_PATH = "/api/v1/silicon-backups/"
UPLOAD_TIMEOUT = 180


def _instance_root(start: str | os.PathLike | None = None) -> Path:
    if start:
        return Path(start).resolve()
    return Path(__file__).resolve().parents[1]


def _load_glass_config(root: Path) -> dict:
    path = root / ".glass.json"
    if not path.exists():
        raise FileNotFoundError("No .glass.json found; this silicon is not connected to Glass.")
    return json.loads(path.read_text())


def _api_key(config: dict) -> str:
    return config.get("api_key") or config.get("silicon_api_key") or ""


def _server_url(config: dict) -> str:
    return (config.get("server_url") or "https://glass.teamofsilicons.com").rstrip("/")


def read_manifest(root: Path) -> list[str]:
    path = root / MANIFEST_NAME
    if not path.exists():
        return []
    patterns: list[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _resolve(root: Path, pattern: str) -> list[Path]:
    raw = Path(os.path.expanduser(pattern))
    if not raw.is_absolute():
        raw = root / raw
    return [Path(p).resolve() for p in glob.glob(str(raw), recursive=True)]


def build_archive(root: Path, patterns: list[str]) -> tuple[bytes, list[str]]:
    root = root.resolve()
    resolved: list[Path] = []
    for pattern in patterns:
        resolved.extend(_resolve(root, pattern))

    existing = sorted({p for p in resolved if p.exists()}, key=lambda p: str(p))
    dirs = [p for p in existing if p.is_dir()]
    top = [
        p
        for p in existing
        if not any(p != d and str(p).startswith(str(d) + os.sep) for d in dirs)
    ]

    buf = io.BytesIO()
    included: list[str] = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in top:
            arcname = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
            try:
                tar.add(path, arcname=arcname)
                included.append(arcname)
            except Exception:
                pass
    buf.seek(0)
    return buf.getvalue(), included


def run_backup(start: str | os.PathLike | None = None, note: str = "on-demand", logger=print) -> bool:
    root = _instance_root(start)
    patterns = read_manifest(root)
    if not patterns:
        logger("backup: no .backupsilicon manifest; nothing to back up")
        return False

    data, included = build_archive(root, patterns)
    if not included:
        logger("backup: .backupsilicon matched no files")
        return False

    config = _load_glass_config(root)
    key = _api_key(config)
    if not key:
        raise ValueError(".glass.json does not contain an api_key")

    response = requests.post(
        _server_url(config) + UPLOAD_PATH,
        headers={"X-Silicon-Key": key},
        files={"file": ("backup.tar.gz", data, "application/gzip")},
        data={"manifest": json.dumps(included), "note": note},
        timeout=UPLOAD_TIMEOUT,
    )
    if response.status_code in {200, 201}:
        try:
            seq = response.json().get("seq", "?")
        except Exception:
            seq = "?"
        logger(f"backup: uploaded v{seq}")
        return True

    logger(f"backup: upload failed HTTP {response.status_code}: {response.text[:200]}")
    return False
