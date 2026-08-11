"""Where the sidecar lives, who it is, and how it reconnects.

Everything the connection needs before it can be opened: the instance root, the
credentials, the websocket URL, the TLS context, and the backoff that decides
when to try again. An authentication rejection backs off far harder than a
dropped connection, because retrying a bad key fast is how a Silicon gets
rate-limited out of its own team.
"""
from __future__ import annotations

import json
import os
import secrets
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from helpers.paths import CODE_ROOT, DATA_ROOT

MAX_BACKOFF = 30
STABLE_CONNECTION_SECONDS = 30
AUTH_REJECTION_BACKOFF = 5 * 60


def silicon_dir() -> Path:
    return DATA_ROOT


def release_dir(root: Path) -> Path:
    """Return active code for the real instance, preserving explicit test roots."""

    try:
        return CODE_ROOT if Path(root).resolve() == DATA_ROOT else Path(root)
    except OSError:
        return Path(root)


def local_bin_dir(root: Path) -> Path:
    return root / ".local" / "bin"


def prepend_local_bin(root: Path) -> None:
    bin_dir = str(local_bin_dir(root))
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in parts:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def load_config(root: Path) -> dict:
    # Share the runtime's exact-root, no-symlink loader so the sidecar cannot
    # inherit another Silicon's credentials and legacy 0644 files are hardened
    # to owner-only permissions before use.
    from interface.config import load_config

    try:
        config, _path = load_config(root)
    except FileNotFoundError:
        return {}
    return config


def api_key_from_config(config: dict) -> str:
    """Return either supported spelling of the per-Silicon credential."""
    return str(config.get("api_key") or config.get("silicon_api_key") or "").strip()


def silicon_name(root: Path) -> str:
    path = root / "silicon.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("address") or data.get("name") or root.name
        except Exception:
            pass
    return root.name


def local_version(root: Path) -> str:
    path = release_dir(root) / "silicon.info"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("version", "")
        except Exception:
            pass
    return ""


def ws_url(server_url: str) -> str:
    """Build a credential-safe Glass agent URL.

    The agent always authenticates this socket with a permanent Silicon key, so
    plaintext WebSockets are allowed only for loopback development servers.
    Reject URL features that could obscure the authenticated destination.
    """

    from interface.config import InterfaceConfigError, validate_authenticated_origin

    try:
        validated = validate_authenticated_origin(server_url)
        parsed = urlsplit(validated)
    except (InterfaceConfigError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Refusing to send a Silicon API key to an unsafe Glass WebSocket URL."
        ) from exc

    websocket_scheme = "wss" if parsed.scheme.lower() == "https" else "ws"
    websocket_path = f"{parsed.path.rstrip('/')}/ws/glass/agent/"
    return urlunsplit(
        parsed._replace(
            scheme=websocket_scheme,
            path=websocket_path,
            query="",
            fragment="",
        )
    )


def is_authentication_rejection(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    return status in {401, 403}


def reconnect_delay(
    backoff: int,
    *,
    rejected: bool,
    session_seconds: float | None,
) -> tuple[int, int]:
    """Return (seconds to wait now, backoff to carry into the next failure).

    `session_seconds` is how long the connection that just broke stayed up, or
    None if it never handshaked. A connection counts as healthy only once it
    outlives STABLE_CONNECTION_SECONDS; anything shorter -- including a socket
    that opens, handshakes, and dies within a second -- is a failed attempt and
    escalates. Without that, a persistent server-side fault pins the agent in a
    fixed-interval reconnect loop that hammers Glass indefinitely.
    """

    if rejected:
        return AUTH_REJECTION_BACKOFF, 1
    if session_seconds is not None and session_seconds >= STABLE_CONNECTION_SECONDS:
        # A healthy connection broke: retry promptly rather than inheriting a
        # delay from failures that predate it.
        return 1, 2
    escalated = min(max(backoff, 1) * 2, MAX_BACKOFF)
    return escalated, escalated


def wait_for_retry(
    root: Path,
    running: list[bool],
    delay: int,
    rejected_key: str = "",
    rejected_server_url: str | None = None,
) -> None:
    """Wait interruptibly, optionally waking when credentials are repaired."""

    deadline = time.monotonic() + max(0, delay)
    while running[0] and time.monotonic() < deadline:
        try:
            current_config = load_config(root)
            current_key = api_key_from_config(current_config)
            current_server_url = str(current_config.get("server_url") or "")
        except (OSError, ValueError, TypeError):
            current_key = ""
            current_server_url = ""
        if current_key and current_server_url:
            if rejected_server_url is not None and (
                not secrets.compare_digest(current_key, rejected_key)
                or current_server_url != rejected_server_url
            ):
                return
            if rejected_key and not secrets.compare_digest(current_key, rejected_key):
                return
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def detect_status(root: Path) -> str:
    pid_file = root / ".silicon.pid"
    stop_file = root / ".silicon.stop"
    if not pid_file.exists():
        return "stopped"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return "running"
    except (ValueError, ProcessLookupError, PermissionError):
        return "stopped" if stop_file.exists() else "crashed"


# Glass runs uvicorn with --ws-max-size 131072. Anything larger is refused at
# the transport with close 1009 and the server application never sees it, so it
# can neither answer nor reject the frame -- the sidecar just loses its socket.
# Callers that can produce bulk (diagnostic rollups) bound themselves with real
# domain knowledge; this is the last-resort backstop for every other frame, so
# no future sender can take the control channel down by being too verbose.
