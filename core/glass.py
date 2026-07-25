"""Glass configuration helpers.

Messaging, media, crons, take-back, and remote browser events now move through
Silicon Interface. This module intentionally keeps only Glass config loading
for sidecar and backup code that needs direct Glass HTTP access.
"""
from __future__ import annotations

import ipaddress
import json
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from core.runtime_paths import DATA_ROOT

CONFIG_FILE = ".glass.json"
PROJECT_ROOT = DATA_ROOT


class GlassConfigurationError(RuntimeError):
    """Glass credentials or origin are missing or unsafe."""


def validate_authenticated_origin(server: str) -> str:
    """Validate an origin before any permanent Silicon credential is sent."""

    value = str(server or "").rstrip("/")
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.lower().rstrip(".") == "localhost"
    secure = parsed.scheme.lower() == "https"
    local_development = parsed.scheme.lower() == "http" and loopback
    if (
        not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not (secure or local_development)
    ):
        raise GlassConfigurationError(
            "Refusing to send a Silicon API key to an unsafe Glass URL."
        )
    return value


def find_glass_config(start: str | Path | None = None) -> Path | None:
    """Return only the credentials owned by the requested Silicon root."""
    current = Path(start or PROJECT_ROOT).resolve()
    path = current / CONFIG_FILE
    return path if path.exists() or path.is_symlink() else None


def _read_local_glass_config(path: Path) -> dict:
    """Read a regular config without following a credential-file symlink."""
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise GlassConfigurationError(
            f"{CONFIG_FILE} must be a local regular file."
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise GlassConfigurationError(
            f"{CONFIG_FILE} must be a local regular file."
        )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GlassConfigurationError(
            f"{CONFIG_FILE} must be a local regular file."
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not os.path.samestat(before, metadata)
        ):
            raise GlassConfigurationError(
                f"{CONFIG_FILE} changed while it was being opened."
            )
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            config = json.load(handle)
            if not isinstance(config, dict):
                raise GlassConfigurationError(
                    f"{CONFIG_FILE} must contain a JSON object."
                )
            return config
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_glass_config(start: str | Path | None = None) -> tuple[dict, Path]:
    path = find_glass_config(start)
    if path is None:
        raise FileNotFoundError("No .glass.json found in this Silicon root.")
    return _read_local_glass_config(path), path


def server_and_key(
    config: dict | None = None,
    *,
    start: str | Path | None = None,
) -> tuple[str, str]:
    """The Glass base URL and this silicon's API key, from .glass.json."""
    if config is None:
        config, _ = load_glass_config(start)
    server = (config.get("server_url") or "").rstrip("/")
    key = str(config.get("api_key") or config.get("silicon_api_key") or "").strip()
    return str(server), key


def authenticated_server_url(
    config: dict | None = None,
    *,
    start: str | Path | None = None,
) -> str:
    """Return a credential-safe Glass base URL.

    Permanent Silicon keys may be sent only over HTTPS, except to a loopback
    HTTP server used for local development. Embedded credentials, query strings,
    and fragments are rejected so requests cannot accidentally disclose a key.
    """

    server, _key = server_and_key(config, start=start)
    return validate_authenticated_origin(server)


def silicon_api_request(
    method: str,
    path: str,
    *,
    config: dict | None = None,
    start: str | Path | None = None,
    json_body: Any | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    files: Any | None = None,
    form_data: Any | None = None,
    timeout: int | float = 15,
):
    """Make one authenticated Silicon API request without following redirects.

    The response is returned untouched so callers can handle conditional
    responses such as 304 and inspect ETag headers. HTTP error responses are not
    raised here; protocol-specific callers decide which statuses are expected.
    Network errors and unsafe/missing configuration still raise.
    """

    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Glass API path must be an absolute-path reference.")
    parsed_path = urlsplit(path)
    if parsed_path.scheme or parsed_path.netloc or parsed_path.fragment:
        raise ValueError("Glass API path must not contain an origin or fragment.")

    if config is None:
        config, _ = load_glass_config(start)
    server, key = server_and_key(config)
    if not server or not key:
        raise GlassConfigurationError(
            "Glass server_url/api_key not configured in .glass.json"
        )
    server = authenticated_server_url(config)

    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(
            {
                str(name): str(value)
                for name, value in headers.items()
                if value is not None
                and str(name).strip().lower()
                not in {"authorization", "x-silicon-key"}
            }
        )
    # The authenticated principal is never caller-overridable.
    request_headers["X-Silicon-Key"] = key
    request_kwargs: dict[str, Any] = {
        "headers": request_headers,
        "json": json_body,
        "params": params,
        "timeout": timeout,
        "allow_redirects": False,
    }
    if files is not None:
        request_kwargs["files"] = files
    if form_data is not None:
        request_kwargs["data"] = form_data
    return requests.request(
        str(method or "GET").upper(),
        f"{server}{path}",
        **request_kwargs,
    )


def silicon_api_post(path: str, json_body: dict | None = None, timeout: int = 15):
    """POST to the Glass API authenticated as this silicon (X-Silicon-Key).

    Same auth the interface CLI uses, so the stemcell can hit silicon-only
    endpoints directly without shelling out to the CLI. Raises on failure.
    """
    resp = silicon_api_request(
        "POST",
        path,
        json_body=json_body or {},
        timeout=timeout,
    )
    if 300 <= int(resp.status_code) < 400:
        raise requests.HTTPError(
            "Glass redirected an authenticated Silicon API request.",
            response=resp,
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
        resp = silicon_api_request(
            "GET",
            "/api/v1/silicons/me/provider-keys",
            config=config,
            timeout=15,
        )
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            raise requests.HTTPError(
                "Glass provider-key request was not accepted.",
                response=resp,
            )
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
