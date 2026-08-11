"""The only place this package talks to Glass.

One request helper, one credential snapshot, one origin validator. Keeping the
wire in one module is what makes 'did this run against the credentials it
started with' a question with one answer.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import paths as paths_module
from pathlib import Path
from typing import Any
from interface.config import (
    CONFIG_FILE,
    LEGACY_CONFIG_FILE,
    authenticated_server_url,
    load_config,
    silicon_api_request,
)


def _response_json(response: Any, operation: str) -> dict[str, Any]:
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise errors_module.TeamContextError(f"Glass returned invalid JSON for {operation}.") from exc
    if not isinstance(body, dict):
        raise errors_module.TeamContextError(f"Glass returned an invalid object for {operation}.")
    return body


def _expect_status(response: Any, expected: set[int], operation: str) -> int:
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in expected:
        raise errors_module.TeamContextError(
            f"Glass {operation} failed with HTTP {status or 'unknown'}.",
            status_code=status or None,
        )
    return status


def _etag(response: Any) -> str:
    headers = getattr(response, "headers", {}) or {}
    value = str(headers.get("ETag") or headers.get("etag") or "")
    if len(value) > 512 or "\r" in value or "\n" in value:
        return ""
    return value


def _request(
    root: Path,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    timeout: int | float = 10,
) -> Any:
    return silicon_api_request(
        method,
        path,
        config=config,
        start=root,
        headers=headers,
        json_body=body,
        timeout=timeout,
    )


def _validated_server_origin(value: Any) -> str:
    origin = str(value or "").strip()
    if not origin or len(origin) > 2048:
        raise errors_module.TeamContextError("Glass returned an invalid server origin.")
    try:
        validated = authenticated_server_url({"server_url": origin})
    except Exception as exc:
        raise errors_module.TeamContextError("Glass returned an invalid server origin.") from exc
    if validated != origin.rstrip("/"):
        raise errors_module.TeamContextError("Glass returned an invalid server origin.")
    return validated


def _credential_fingerprint(config: dict[str, Any]) -> str:
    key = str(
        config.get("api_key") or config.get("silicon_api_key") or ""
    ).strip()
    if not key:
        raise errors_module.TeamContextError("Glass configuration does not contain a Silicon key.")
    return paths_module._sha256(b"team-context-credential\0" + key.encode("utf-8"))


def _validated_credential_fingerprint(
    value: Any,
    *,
    allow_empty: bool = False,
) -> str:
    fingerprint = str(value or "").lower()
    if allow_empty and not fingerprint:
        return ""
    if not constants._SHA256_RE.fullmatch(fingerprint):
        raise errors_module.TeamContextError("Glass identity has an invalid credential scope.")
    return fingerprint


def _load_config_snapshot(root: Path) -> tuple[dict[str, Any], str]:
    config, config_path = load_config(root)
    if (
        config_path.name not in {CONFIG_FILE, LEGACY_CONFIG_FILE}
        or config_path.parent.resolve() != root.resolve()
    ):
        raise errors_module.TeamContextError(
            f"This Silicon does not have its own {CONFIG_FILE} configuration."
        )
    if not isinstance(config, dict):
        raise errors_module.TeamContextError("Glass configuration must be a JSON object.")
    try:
        origin = authenticated_server_url(config)
    except Exception as exc:
        raise errors_module.TeamContextError(
            "Glass configuration has an unsafe server origin."
        ) from exc
    return config, _validated_server_origin(origin)
