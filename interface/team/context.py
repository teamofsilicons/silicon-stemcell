"""The team document itself: validating it, and asking for it conditionally.

A payload is checked against the identity scope it claims before any of it is
believed, and the GET is conditional on the revision already held.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import http as http_module
from interface.team import manifest as manifest_module
from interface.team import paths as paths_module
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _validate_context_payload(
    payload: dict[str, Any],
    identity: dict[str, str],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if payload.get("path") != constants.TEAM_CONTEXT_PATH:
        raise errors_module.TeamContextError("Glass returned an invalid TEAM.md path.")
    if str(payload.get("team_slug") or "") != identity["team_slug"]:
        raise errors_module.TeamContextError("Glass returned context for the wrong team.")
    markdown = payload.get("markdown")
    if not isinstance(markdown, str):
        raise errors_module.TeamContextError("Glass returned invalid TEAM.md content.")
    markdown_bytes = markdown.encode("utf-8")
    if len(markdown_bytes) > constants.MAX_TEAM_CONTEXT_BYTES:
        raise errors_module.TeamContextError(
            f"Glass TEAM.md exceeds {constants.MAX_TEAM_CONTEXT_BYTES} UTF-8 bytes."
        )
    revision = str(payload.get("revision") or "").lower()
    if not constants._SHA256_RE.fullmatch(revision) or paths_module._sha256(markdown_bytes) != revision:
        raise errors_module.TeamContextError("Glass returned a TEAM.md hash mismatch.")
    sync_revision = str(payload.get("sync_revision") or "").lower()
    if not constants._SHA256_RE.fullmatch(sync_revision):
        raise errors_module.TeamContextError("Glass returned an invalid team sync revision.")
    manifest = manifest_module._normalise_manifest(payload.get("advertising_memories"))
    if identity["silicon_id"] not in manifest:
        raise errors_module.TeamContextError("Glass team context does not contain this Silicon.")
    team_id = str(payload.get("team_id") or "")
    if len(team_id) > 255:
        raise errors_module.TeamContextError("Glass returned an invalid team ID.")
    context = {
        "team_id": team_id,
        "team_slug": identity["team_slug"],
        "path": constants.TEAM_CONTEXT_PATH,
        "revision": revision,
        "sync_revision": sync_revision,
        "markdown": markdown,
    }
    return context, manifest


def _manifest_for_state(manifest: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [manifest[silicon_id] for silicon_id in sorted(manifest)]


def _manifest_from_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    context = state.get("context") or {}
    return manifest_module._normalise_manifest(context.get("advertising_memories"))


def _context_matches_identity_scope(
    state: dict[str, Any],
    identity: dict[str, Any],
) -> bool:
    context = state.get("context")
    return bool(
        isinstance(context, dict)
        and context.get("team_slug") == identity["team_slug"]
        and context.get("server_origin") == identity["server_origin"]
        and context.get("credential_fingerprint")
        == identity["credential_fingerprint"]
    )


def _get_context_response(
    root: Path,
    identity: dict[str, Any],
    state: dict[str, Any],
    *,
    force: bool,
    config: dict[str, Any] | None = None,
) -> Any:
    headers: dict[str, str] = {}
    etag = str((state.get("context") or {}).get("etag") or "")
    if etag and not force and _context_matches_identity_scope(state, identity):
        headers["If-None-Match"] = etag
    slug = quote(identity["team_slug"], safe="")
    return http_module._request(
        root,
        "GET",
        f"/api/v1/teams/{slug}/silicon-context",
        headers=headers,
        config=config,
        timeout=12,
    )
