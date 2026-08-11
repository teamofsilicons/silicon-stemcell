"""One manifest entry, validated.

A leaf so a peer can check its own deletion authority without importing the
context that fetched the manifest.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import paths as paths_module
from typing import Any


def _manifest_entry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise errors_module.TeamContextError("Glass returned an invalid advertising manifest entry.")
    silicon_id = paths_module._validate_identifier(raw.get("silicon_id"), "Silicon ID")
    expected_path = f"{constants.ADVERTISING_DIRECTORY}/{silicon_id}.md"
    if raw.get("path") != expected_path:
        raise errors_module.TeamContextError("Glass returned an invalid advertising-memory path.")
    revision = raw.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise errors_module.TeamContextError("Glass returned an invalid advertising-memory revision.")
    digest = str(raw.get("sha256") or "").lower()
    if not constants._SHA256_RE.fullmatch(digest):
        raise errors_module.TeamContextError("Glass returned an invalid advertising-memory hash.")
    updated_at = raw.get("updated_at")
    if updated_at is not None and (
        not isinstance(updated_at, str) or len(updated_at) > 100
    ):
        raise errors_module.TeamContextError(
            "Glass returned an invalid advertising-memory timestamp."
        )
    return {
        "silicon_id": silicon_id,
        "path": expected_path,
        "revision": revision,
        "sha256": digest,
        "updated_at": updated_at,
    }


def _normalise_manifest(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        raise errors_module.TeamContextError("Glass returned an invalid advertising-memory manifest.")
    manifest: dict[str, dict[str, Any]] = {}
    for item in raw:
        entry = _manifest_entry(item)
        silicon_id = entry["silicon_id"]
        if silicon_id in manifest:
            raise errors_module.TeamContextError(
                "Glass returned duplicate advertising-memory entries."
            )
        manifest[silicon_id] = entry
    return manifest
