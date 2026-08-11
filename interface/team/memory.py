"""Advertising memory: what a Silicon tells its team it can do.

Both directions are bounded and validated — what this Silicon writes locally,
and what Glass says a peer wrote. A memory that exceeds its limits is refused
rather than truncated.
"""
from __future__ import annotations

from interface.team import constants
from interface.team import errors as errors_module
from interface.team import paths as paths_module
from pathlib import Path
from typing import Any


def _validate_memory(content: str, max_lines: int, max_bytes: int, oversize: str) -> str:
    if not isinstance(content, str):
        raise ValueError("Advertising memory content must be a string.")
    if "\x00" in content:
        raise ValueError("Advertising memory cannot contain NUL characters.")
    if len(content.splitlines()) > max_lines:
        raise ValueError(oversize.format(limit=max_lines, unit="lines"))
    if len(content.encode("utf-8")) > max_bytes:
        raise ValueError(oversize.format(limit=max_bytes, unit="UTF-8 bytes"))
    return content


def validate_advertising_memory(content: str) -> str:
    """Validate the exact Glass advertising-memory limits without truncating."""
    return _validate_memory(
        content,
        constants.MAX_ADVERTISING_MEMORY_LINES,
        constants.MAX_ADVERTISING_MEMORY_BYTES,
        "Advertising memory cannot exceed {limit} {unit}.",
    )


def validate_advertised_memory(content: str) -> str:
    """Validate a Glass-composed peer memory with its managed integration block."""
    return _validate_memory(
        content,
        constants.MAX_ADVERTISED_MEMORY_LINES,
        constants.MAX_ADVERTISED_MEMORY_BYTES,
        "Advertised memory cannot exceed {limit} {unit}.",
    )


def _read_local_memory(
    root: Path,
    path: Path,
    *,
    allow_managed: bool = False,
) -> tuple[str, str]:
    try:
        raw = paths_module._read_regular_bytes(
            root,
            path,
            max_bytes=(
                constants.MAX_ADVERTISED_MEMORY_BYTES
                if allow_managed
                else constants.MAX_ADVERTISING_MEMORY_BYTES
            ),
        )
    except (ValueError, errors_module.TeamContextError) as exc:
        if isinstance(exc, errors_module.TeamContextError):
            raise ValueError(
                "Advertising memory path must remain inside the Silicon root."
            ) from exc
        if "size limit" in str(exc):
            maximum = (
                constants.MAX_ADVERTISED_MEMORY_BYTES
                if allow_managed
                else constants.MAX_ADVERTISING_MEMORY_BYTES
            )
            raise ValueError(
                f"Advertising memory cannot exceed {maximum} UTF-8 bytes."
            ) from exc
        raise
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Advertising memory must be valid UTF-8.") from exc
    (
        validate_advertised_memory(content)
        if allow_managed
        else validate_advertising_memory(content)
    )
    return content, paths_module._sha256(raw)


def _validate_memory_payload(
    payload: dict[str, Any],
    silicon_id: str,
    *,
    expected: dict[str, Any] | None = None,
    allow_managed: bool = False,
) -> dict[str, Any]:
    if str(payload.get("silicon_id") or "") != silicon_id:
        raise errors_module.TeamContextError(
            "Glass returned advertising memory for the wrong Silicon."
        )
    expected_path = f"{constants.ADVERTISING_DIRECTORY}/{silicon_id}.md"
    if payload.get("path") != expected_path:
        raise errors_module.TeamContextError("Glass returned an invalid advertising-memory path.")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise errors_module.TeamContextError("Glass returned an invalid advertising-memory revision.")
    content = payload.get("content")
    try:
        (
            validate_advertised_memory(content)
            if allow_managed
            else validate_advertising_memory(content)
        )
    except ValueError as exc:
        raise errors_module.TeamContextError(str(exc)) from exc
    digest = str(payload.get("sha256") or "").lower()
    actual_digest = paths_module._sha256(content.encode("utf-8"))
    if not constants._SHA256_RE.fullmatch(digest) or digest != actual_digest:
        raise errors_module.TeamContextError("Glass returned an advertising-memory hash mismatch.")
    if expected and (expected["revision"] != revision or expected["sha256"] != digest):
        raise errors_module.TeamContextError("Glass advertising memory does not match its manifest.")
    updated_at = payload.get("updated_at")
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
        "content": content,
    }
