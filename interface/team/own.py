"""This Silicon's own advertising memory, as the server holds it.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import http as http_module
from interface.team import memory as memory_module
from pathlib import Path
from typing import Any


def _fetch_own_remote(
    root: Path,
    identity: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    response = http_module._request(
        root,
        "GET",
        "/api/v1/silicons/me/advertising-memory",
        config=config,
        timeout=10,
    )
    http_module._expect_status(response, {200}, "own advertising-memory request")
    payload = http_module._response_json(response, "own advertising-memory request")
    return memory_module._validate_memory_payload(payload, identity["silicon_id"]), http_module._etag(response)


def _set_own_base(
    state: dict[str, Any],
    identity: dict[str, Any],
    remote: dict[str, Any],
    *,
    etag: str = "",
) -> None:
    state["own"] = {
        "silicon_id": identity["silicon_id"],
        "base_revision": remote["revision"],
        "base_sha256": remote["sha256"],
        "etag": etag,
        "status": "synced",
    }


def _mark_own_conflict(
    state: dict[str, Any],
    identity: dict[str, Any],
    *,
    pending_sha256: str,
    actual_revision: int | None,
    remote_sha256: str = "",
) -> None:
    own = state.setdefault("own", {})
    own.update(
        {
            "silicon_id": identity["silicon_id"],
            "status": "conflict",
            "pending_sha256": pending_sha256,
        }
    )
    existing_revision = own.get("conflict_revision")
    valid_actual = (
        isinstance(actual_revision, int)
        and not isinstance(actual_revision, bool)
        and actual_revision >= 0
    )
    valid_existing = (
        isinstance(existing_revision, int)
        and not isinstance(existing_revision, bool)
        and existing_revision >= 0
    )
    if valid_actual and (not valid_existing or actual_revision >= existing_revision):
        own["conflict_revision"] = actual_revision
    if (
        constants._SHA256_RE.fullmatch(remote_sha256)
        and valid_actual
        and own.get("conflict_revision") == actual_revision
    ):
        own["conflict_sha256"] = remote_sha256


def _own_conflict_result(own: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "status": "conflict",
        "changed": False,
        "local_saved": True,
    }
    actual_revision = own.get("conflict_revision")
    if isinstance(actual_revision, int) and not isinstance(actual_revision, bool):
        result["actual_revision"] = actual_revision
    return result
