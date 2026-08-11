"""Read-only release checks and Silicon credential maintenance.

Source mutation is owned exclusively by the host ``silicon-cli`` transactional
updater. The running Stemcell may discover and report a newer release, but it
never rewrites its own code, delegates mutation to another agent, or changes
Git configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from core.glass import GlassConfigurationError, validate_authenticated_origin
from core.runtime_paths import CODE_ROOT, DATA_ROOT
from core.state_store import atomic_write_bytes, file_lock

PROJECT_ROOT = DATA_ROOT
DOTENV_FILE = PROJECT_ROOT / ".env"
ENV_PY_FILE = PROJECT_ROOT / "env.py"
GLASS_CONFIG_FILE = PROJECT_ROOT / ".glass.json"
SILICON_CONFIG_FILE = PROJECT_ROOT / "silicon.json"
SILICON_INFO_FILE = CODE_ROOT / "silicon.info"
UPDATE_STATE_FILE = PROJECT_ROOT / "core" / "interface_state" / "system_update.json"

DEFAULT_GLASS_SERVER_URL = "https://glass.teamofsilicons.com"
DEFAULT_STEMCELL_REPO = "teamofsilicons/silicon-stemcell"
UPDATE_CHECK_INTERVAL_SECONDS = 60 * 60
AUTH_KEY_PATH = "/api/v1/silicon-version/auth-key"
AUTH_IDENTITY_PATH = "/api/v1/silicons/me"
REQUEST_TIMEOUT = 30
GIT_TIMEOUT = 45
MAX_GIT_RELEASE_REFS = 100_000
MAX_GIT_RELEASE_METADATA_BYTES = 16 * 1024 * 1024
PENDING_AUTH_KEY_NAME = "SILICON_UPDATE_PENDING_AUTH_KEY"
AUTH_KEY_LOCK_NAME = ".silicon-update-auth-key"
_SILICON_KEY_TEXT_RE = re.compile(r"scs_live_[A-Za-z0-9_-]+")
_STABLE_TAG_RE = re.compile(
    r"\Av(0|[1-9][0-9]{0,2})\."
    r"(0|[1-9][0-9]{0,2})\."
    r"(0|[1-9][0-9]{0,2})\Z"
)
_STABLE_VERSION_RE = re.compile(
    r"\A(0|[1-9][0-9]{0,2})\."
    r"(0|[1-9][0-9]{0,2})\."
    r"(0|[1-9][0-9]{0,2})\Z"
)


class UpdateAuthenticationError(RuntimeError):
    """The updater cannot authenticate; an owner must reprovision the Silicon."""


def _safe_error_text(exc: Exception) -> str:
    """Bound and redact operator-facing failures; credentials never reach logs."""

    text = str(exc).replace("\r", " ").replace("\n", " ")
    text = _SILICON_KEY_TEXT_RE.sub("[REDACTED SILICON KEY]", text)
    try:
        configured_secrets = {_auth_key(), _pending_auth_key()}
    except Exception:
        configured_secrets = set()
    for configured_secret in sorted(
        (secret for secret in configured_secrets if secret),
        key=len,
        reverse=True,
    ):
        text = text.replace(configured_secret, "[REDACTED SILICON KEY]")
    return text[:500]


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_dotenv(path: Path | None = None) -> dict[str, str]:
    path = DOTENV_FILE if path is None else path
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def _read_env_py(path: Path | None = None) -> dict[str, str]:
    path = ENV_PY_FILE if path is None else path
    values: dict[str, str] = {}
    if not path.exists():
        return values
    pattern = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(['\"])(.*?)\2\s*$")
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(raw)
        if match:
            values[match.group(1)] = match.group(3)
    return values


def _write_secret(path: Path, content: str) -> None:
    """Atomically replace a local credential file with owner-only durability."""
    atomic_write_bytes(path, content.encode("utf-8"), mode=0o600, dir_mode=None)


def _upsert_key_value(path: Path, key: str, value: str, *, python_string: bool = False) -> None:
    if python_string:
        rendered = f"{key} = {json.dumps(value)}"
    else:
        rendered = f"{key}={value}"

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    out: list[str] = []
    for line in lines:
        if pattern.match(line):
            out.append(rendered)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(rendered)

    _write_secret(path, "\n".join(out).rstrip() + "\n")


def _replace_json_auth_key(
    path: Path,
    auth_key: str,
    *,
    nested: bool = False,
    create: bool = False,
    defaults: dict[str, Any] | None = None,
) -> None:
    if not path.exists():
        if not create:
            return
        payload: Any = dict(defaults or {})
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"Cannot safely update malformed credential file {path.name}."
            ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Cannot safely update malformed credential file {path.name}.")

    target = payload
    if nested:
        existing = payload.get("glass")
        if existing is None:
            return
        if not isinstance(existing, dict):
            raise RuntimeError(f"Cannot safely update malformed credential file {path.name}.")
        target = existing

    target["api_key"] = auth_key
    if "silicon_api_key" in target:
        target["silicon_api_key"] = auth_key
    _write_secret(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _remove_key_value(path: Path, key: str) -> None:
    if not path.exists():
        return
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not pattern.match(line)
    ]
    _write_secret(path, "\n".join(lines).rstrip() + ("\n" if lines else ""))


def _remove_json_auth_keys(path: Path, *, nested: bool = False) -> None:
    if not path.exists():
        return
    payload = _read_json(path, None)
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Cannot safely scrub malformed credential file {path.name}."
        )
    target = payload
    if nested:
        target = payload.get("glass")
        if not isinstance(target, dict):
            return
    changed = False
    for key in ("api_key", "silicon_api_key"):
        if key in target:
            target.pop(key, None)
            changed = True
    if changed:
        _write_secret(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _canonical_glass_defaults() -> dict[str, str]:
    """Carry legacy origin/identity metadata into a newly canonical config."""

    silicon = _silicon_config()
    nested = silicon.get("glass") if isinstance(silicon.get("glass"), dict) else {}
    defaults = {"server_url": _authenticated_server_url()}
    candidates = {
        "silicon_id": (
            nested.get("silicon_id"),
            silicon.get("silicon_id"),
        ),
        "silicon_username": (
            nested.get("silicon_username"),
            silicon.get("silicon_username"),
            silicon.get("address"),
        ),
        "address": (
            nested.get("address"),
            silicon.get("address"),
        ),
    }
    for key, values in candidates.items():
        value = next(
            (
                str(candidate).strip()
                for candidate in values
                if candidate and str(candidate).strip()
            ),
            "",
        )
        if value:
            defaults[key] = value
    return defaults


def _persist_auth_key(auth_key: str) -> None:
    if not auth_key:
        return
    # `.glass.json` is the canonical source for messaging, backup, remote
    # browser, and provider-key traffic. Update it first once Glass has proven
    # the candidate active. The ignored pending dotenv entry remains a crash
    # journal until legacy duplicate locations have been scrubbed.
    _replace_json_auth_key(
        GLASS_CONFIG_FILE,
        auth_key,
        create=True,
        defaults=_canonical_glass_defaults(),
    )
    _remove_json_auth_keys(SILICON_CONFIG_FILE, nested=True)
    _remove_key_value(DOTENV_FILE, "SILICON_UPDATE_AUTH_KEY")
    _remove_key_value(DOTENV_FILE, "GLASS_API_KEY")
    if ENV_PY_FILE.exists():
        _remove_key_value(ENV_PY_FILE, "SILICON_UPDATE_AUTH_KEY")
        _upsert_key_value(ENV_PY_FILE, "GLASS_API_KEY", "", python_string=True)


def _pending_auth_key() -> str:
    return str(_read_dotenv().get(PENDING_AUTH_KEY_NAME) or "").strip()


def _stage_auth_key(auth_key: str) -> None:
    _upsert_key_value(DOTENV_FILE, PENDING_AUTH_KEY_NAME, auth_key)


def _clear_pending_auth_key() -> None:
    _remove_key_value(DOTENV_FILE, PENDING_AUTH_KEY_NAME)


def _glass_config() -> dict[str, Any]:
    return _read_json(GLASS_CONFIG_FILE, {})


def _silicon_config() -> dict[str, Any]:
    return _read_json(SILICON_CONFIG_FILE, {})


def _configured_auth_pair() -> tuple[str, str]:
    """Return an origin/key pair from one credential authority.

    Once `.glass.json` contains a key it is canonical for both values; stale
    legacy environment entries must never redirect that canonical credential.
    """

    dotenv = _read_dotenv()
    env_py = _read_env_py()
    glass = _glass_config()
    silicon = _silicon_config()
    nested_glass = silicon.get("glass") if isinstance(silicon.get("glass"), dict) else {}

    glass_key = str(
        glass.get("api_key") or glass.get("silicon_api_key") or ""
    ).strip()
    if glass_key:
        return str(glass.get("server_url") or "").rstrip("/"), glass_key

    nested_key = str(
        nested_glass.get("api_key")
        or nested_glass.get("silicon_api_key")
        or ""
    ).strip()
    if nested_key:
        server = (
            nested_glass.get("server_url")
            or glass.get("server_url")
            or dotenv.get("GLASS_SERVER_URL")
            or DEFAULT_GLASS_SERVER_URL
        )
        return str(server).rstrip("/"), nested_key

    dotenv_key = str(
        dotenv.get("SILICON_UPDATE_AUTH_KEY")
        or dotenv.get("GLASS_API_KEY")
        or ""
    ).strip()
    if dotenv_key:
        server = dotenv.get("GLASS_SERVER_URL") or DEFAULT_GLASS_SERVER_URL
        return str(server).rstrip("/"), dotenv_key

    env_py_key = str(
        env_py.get("SILICON_UPDATE_AUTH_KEY")
        or env_py.get("GLASS_API_KEY")
        or ""
    ).strip()
    if env_py_key:
        server = dotenv.get("GLASS_SERVER_URL") or DEFAULT_GLASS_SERVER_URL
        return str(server).rstrip("/"), env_py_key

    environment_key = str(
        os.environ.get("SILICON_UPDATE_AUTH_KEY")
        or os.environ.get("GLASS_API_KEY")
        or ""
    ).strip()
    if environment_key:
        server = os.environ.get("GLASS_SERVER_URL") or DEFAULT_GLASS_SERVER_URL
        return str(server).rstrip("/"), environment_key

    server = (
        glass.get("server_url")
        or nested_glass.get("server_url")
        or dotenv.get("GLASS_SERVER_URL")
        or os.environ.get("GLASS_SERVER_URL")
        or DEFAULT_GLASS_SERVER_URL
    )
    return str(server).rstrip("/"), ""


def _server_url() -> str:
    return _configured_auth_pair()[0]


def _authenticated_server_url() -> str:
    """Return a credential-safe Glass origin (HTTPS, or local-loopback HTTP)."""

    try:
        return validate_authenticated_origin(_server_url())
    except GlassConfigurationError as exc:
        raise UpdateAuthenticationError(
            "Refusing to send a Silicon API key to a non-HTTPS, non-loopback Glass URL."
        ) from exc


def _auth_key() -> str:
    return _configured_auth_pair()[1]


def _get_identity_with_key(auth_key: str):
    return requests.get(
        _authenticated_server_url() + AUTH_IDENTITY_PATH,
        headers={"X-Silicon-Key": auth_key},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
    )


def _reject_redirect(response, operation: str) -> None:
    if 300 <= int(response.status_code) < 400:
        raise UpdateAuthenticationError(
            f"Glass redirected the authenticated {operation}; refusing to forward the Silicon key."
        )


def _configured_silicon_identities() -> set[str]:
    glass = _glass_config()
    silicon = _silicon_config()
    nested = silicon.get("glass") if isinstance(silicon.get("glass"), dict) else {}
    return {
        str(value).strip()
        for value in (
            glass.get("silicon_id"),
            glass.get("silicon_username"),
            nested.get("silicon_id"),
            nested.get("silicon_username"),
            silicon.get("silicon_id"),
            silicon.get("address"),
        )
        if value and str(value).strip()
    }


def _identity_response_is_authoritative(response) -> bool:
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except (ValueError, TypeError) as exc:
        raise UpdateAuthenticationError(
            "Glass key verification did not return a valid Silicon identity."
        ) from exc
    silicon_id = str(body.get("silicon_id") or "").strip() if isinstance(body, dict) else ""
    expected = _configured_silicon_identities()
    if not silicon_id or (expected and silicon_id not in expected):
        raise UpdateAuthenticationError(
            "Glass key verification returned the wrong Silicon identity."
        )
    return True


def _recover_pending_auth_key(current_key: str) -> str:
    """Resolve a rotation whose POST response may have been lost.

    The candidate is staged before Glass is called. A 200 from the authenticated
    Silicon identity endpoint proves Glass installed it; a generic 404 is never
    treated as authentication truth.
    """
    pending = _pending_auth_key()
    if not pending:
        return current_key

    candidate_response = _get_identity_with_key(pending)
    _reject_redirect(candidate_response, "key verification")
    if _identity_response_is_authoritative(candidate_response):
        _persist_auth_key(pending)
        _clear_pending_auth_key()
        return pending
    if candidate_response.status_code not in {401, 403}:
        candidate_response.raise_for_status()

    if current_key:
        current_response = _get_identity_with_key(current_key)
        _reject_redirect(current_response, "key verification")
        if _identity_response_is_authoritative(current_response):
            _clear_pending_auth_key()
            return current_key
        if current_response.status_code not in {401, 403}:
            current_response.raise_for_status()

    raise UpdateAuthenticationError(
        "Silicon key rotation is unresolved. Ask the Silicon owner to reprovision its API key."
    )


def _rotate_auth_key_locked() -> str:
    pending_before_recovery = _pending_auth_key()
    current_key = _recover_pending_auth_key(_auth_key())
    if pending_before_recovery and current_key == pending_before_recovery:
        return current_key
    if not current_key:
        raise UpdateAuthenticationError(
            "No Silicon API key is configured. Ask the Silicon owner to reprovision it."
        )

    server_url = _authenticated_server_url()
    replacement = "scs_live_" + secrets.token_urlsafe(32)
    _stage_auth_key(replacement)
    try:
        response = requests.post(
            server_url + AUTH_KEY_PATH,
            headers={"X-Silicon-Key": current_key},
            json={"replacement_key": replacement},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException:
        # The server may have committed before the connection failed. Probe the
        # staged candidate rather than retrying a potentially revoked key.
        recovered = _recover_pending_auth_key(current_key)
        if recovered == replacement:
            return replacement
        raise

    _reject_redirect(response, "key rotation")
    if response.status_code in {200, 201}:
        # Never trust acknowledgement alone: prove the staged key authenticates
        # before replacing every local credential copy.
        recovered = _recover_pending_auth_key(current_key)
        if recovered == replacement:
            return replacement
        raise RuntimeError("Glass acknowledged rotation but the replacement key is not active.")
    if response.status_code in {401, 403}:
        recovered = _recover_pending_auth_key(current_key)
        if recovered == replacement:
            return replacement
        raise UpdateAuthenticationError(
            "Glass rejected the configured Silicon key. Ask the Silicon owner to reprovision it."
        )

    # A concurrent/ambiguous result can still have committed. Resolve it from
    # authentication truth before surfacing the server error.
    recovered = _recover_pending_auth_key(current_key)
    if recovered == replacement:
        return replacement
    response.raise_for_status()
    raise RuntimeError("Glass rejected Silicon key rotation.")


def _rotate_auth_key() -> str:
    """Rotate with the current key, without ever relying on a shared credential."""

    with file_lock(UPDATE_STATE_FILE.parent / AUTH_KEY_LOCK_NAME):
        return _rotate_auth_key_locked()


def _stemcell_git_url() -> str:
    repository = os.environ.get(
        "SILICON_STEMCELL_REPO",
        DEFAULT_STEMCELL_REPO,
    ).strip()
    repository_parts = repository.split("/")
    if (
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        is None
        or len(repository_parts) != 2
        or any(
            part in {".", ".."} or len(part) > 100
            for part in repository_parts
        )
    ):
        raise RuntimeError(
            "SILICON_STEMCELL_REPO must be one GitHub owner/repository pair"
        )
    return f"https://github.com/{repository}.git"


def _git_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "GIT_ALLOW_PROTOCOL",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ASKPASS",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PROXY_COMMAND",
        "GIT_PROTOCOL",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_SSL_NO_VERIFY",
        "GIT_TEMPLATE_DIR",
        "GIT_WORK_TREE",
        "SSH_ASKPASS",
    ):
        environment.pop(key, None)
    for key in tuple(environment):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "7",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "core.hooksPath",
            "GIT_CONFIG_VALUE_1": os.devnull,
            "GIT_CONFIG_KEY_2": "core.autocrlf",
            "GIT_CONFIG_VALUE_2": "false",
            "GIT_CONFIG_KEY_3": "core.eol",
            "GIT_CONFIG_VALUE_3": "lf",
            "GIT_CONFIG_KEY_4": "http.sslVerify",
            "GIT_CONFIG_VALUE_4": "true",
            "GIT_CONFIG_KEY_5": "protocol.allow",
            "GIT_CONFIG_VALUE_5": "never",
            "GIT_CONFIG_KEY_6": "protocol.https.allow",
            "GIT_CONFIG_VALUE_6": "always",
        }
    )
    return environment


def _fetch_latest_version() -> dict[str, Any] | None:
    """Resolve the highest published stable Stemcell Git tag."""

    if shutil.which("git") is None:
        raise RuntimeError("Git is required to check published Silicon releases")
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", _stemcell_git_url()],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT,
            env=_git_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Git release check timed out") from exc
    except UnicodeError as exc:
        raise RuntimeError("Git returned non-UTF-8 release metadata") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not run Git: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            "Could not list published Silicon releases: "
            + (result.stderr.strip() or "Git exited unsuccessfully")
        )

    if len(result.stdout.encode("utf-8")) > MAX_GIT_RELEASE_METADATA_BYTES:
        raise RuntimeError("Git returned too much release metadata")
    references: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        if len(references) >= MAX_GIT_RELEASE_REFS:
            raise RuntimeError("Git returned too many release references")
        fields = raw_line.split()
        if len(fields) != 2:
            raise RuntimeError("Git returned malformed release metadata")
        object_id, reference = fields
        object_id = object_id.lower()
        if (
            len(object_id) != 40
            or any(character not in "0123456789abcdef" for character in object_id)
            or not reference.startswith("refs/tags/")
        ):
            raise RuntimeError("Git returned invalid release metadata")
        previous = references.setdefault(reference, object_id)
        if previous != object_id:
            raise RuntimeError(
                f"Git advertised conflicting objects for {reference}"
            )
    for reference in references:
        if reference.endswith("^{}") and reference[:-3] not in references:
            raise RuntimeError(f"Git advertised an orphan peeled tag: {reference}")

    candidates: list[tuple[tuple[int, int, int], str, str]] = []
    for reference, tag_object in references.items():
        if reference.endswith("^{}"):
            continue
        tag = reference.removeprefix("refs/tags/")
        match = _STABLE_TAG_RE.fullmatch(tag)
        if match is None:
            continue
        parts = tuple(int(value) for value in match.groups())
        revision = references.get(f"{reference}^{{}}", tag_object)
        candidates.append((parts, tag, revision))
    if not candidates:
        return None

    parts, tag, revision = max(candidates, key=lambda candidate: candidate[0])
    return {
        "version": ".".join(str(value) for value in parts),
        "tag": tag,
        "revision": revision,
        "source": _stemcell_git_url(),
    }


def _local_version() -> str:
    info = _read_json(SILICON_INFO_FILE, {})
    return str(info.get("version") or "").strip() if isinstance(info, dict) else ""


def _latest_version_id(latest: dict[str, Any]) -> str:
    return str(latest.get("version_id") or latest.get("version") or "").strip()


def _stable_version(
    value: str,
    *,
    allow_legacy_two_part: bool = False,
) -> tuple[int, int, int] | None:
    text = value
    if allow_legacy_two_part and re.fullmatch(
        r"(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})",
        text,
    ):
        text += ".0"
    match = _STABLE_VERSION_RE.fullmatch(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _check_system_update(now: float | None = None) -> dict[str, Any]:
    """Return release status without mutating the Stemcell source tree."""

    now = time.time() if now is None else now
    state = _read_json(UPDATE_STATE_FILE, {"version": 1})
    last_checked = float(state.get("last_checked_at") or 0)
    if now - last_checked < UPDATE_CHECK_INTERVAL_SECONDS:
        return {
            "status": "throttled",
            "local_version": str(state.get("local_version") or ""),
            "latest_version": str(state.get("latest_seen_version") or ""),
            "update_available": bool(state.get("update_available")),
        }

    state["last_checked_at"] = now
    _write_json(UPDATE_STATE_FILE, state)

    try:
        latest = _fetch_latest_version()
    except Exception as exc:
        safe_error = _safe_error_text(exc)
        state["last_error"] = safe_error
        _write_json(UPDATE_STATE_FILE, state)
        print(f"[Update] Error checking silicon version: {safe_error}", flush=True)
        return {
            "status": "error",
            "local_version": _local_version(),
            "latest_version": "",
            "update_available": False,
            "error": safe_error,
        }

    local_version = _local_version()
    if not latest:
        state.update(
            {
                "local_version": local_version,
                "latest_seen_version": "",
                "update_available": False,
                "last_error": "",
            }
        )
        _write_json(UPDATE_STATE_FILE, state)
        return {
            "status": "unpublished",
            "local_version": local_version,
            "latest_version": "",
            "update_available": False,
        }

    latest_version = _latest_version_id(latest)
    local_parts = _stable_version(
        local_version,
        allow_legacy_two_part=True,
    )
    latest_parts = _stable_version(latest_version)
    available = bool(
        local_parts is not None
        and latest_parts is not None
        and latest_parts > local_parts
    )
    state.update(
        {
            "local_version": local_version,
            "latest_seen_version": latest_version,
            "update_available": available,
            "last_error": "",
        }
    )
    already_notified = str(state.get("last_notified_version") or "")
    if available and already_notified != latest_version:
        state["last_notified_version"] = latest_version
        print(
            f"[Update] {local_version or '?'} → {latest_version} is available. "
            "Run `silicon update <name>`; the CLI will drain, stop, update, "
            "restart, and verify it safely.",
            flush=True,
        )
    elif not available:
        state["last_notified_version"] = ""
    _write_json(UPDATE_STATE_FILE, state)

    return {
        "status": "available" if available else "up_to_date",
        "local_version": local_version,
        "latest_version": latest_version,
        "update_available": available,
    }


def check_for_system_update(now: float | None = None) -> dict[str, str]:
    """Periodic event-loop adapter; checks and records status, never prompts."""

    _check_system_update(now=now)
    return {}


def trigger_system_update_check(*, force: bool = True) -> dict[str, Any]:
    """Run the same update check on demand for CLI-triggered checks."""

    now = time.time() + UPDATE_CHECK_INTERVAL_SECONDS if force else None
    return _check_system_update(now=now)


def apply_update() -> int:
    """Compatibility command that refuses unsafe live source mutation."""

    print(
        "[Update] In-process self-update is disabled. Run "
        "`silicon update <name>` from the host; the CLI performs the "
        "task-safe stop and restart.",
        flush=True,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Silicon release check and credential maintenance."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["check", "apply", "rotate-key"],
        default="check",
        help="check: compare versions without changing source (default). "
        "apply: deprecated safe refusal; use the host silicon-cli updater. "
        "rotate-key: replace the configured key using authenticated "
        "self-rotation.",
    )
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="Respect the hourly throttle instead of forcing the check.",
    )
    args = parser.parse_args(argv)
    if args.command == "apply":
        try:
            return apply_update()
        except Exception as exc:
            print(f"[Update] Apply failed: {_safe_error_text(exc)}", flush=True)
            return 1
    if args.command == "rotate-key":
        try:
            _rotate_auth_key()
        except Exception as exc:
            print(f"[Update] Silicon key rotation failed: {_safe_error_text(exc)}", flush=True)
            return 1
        print("[Update] Silicon API key rotated and stored.", flush=True)
        return 0
    try:
        result = trigger_system_update_check(force=not args.no_force)
    except Exception as exc:
        print(f"[Update] Check failed: {_safe_error_text(exc)}", flush=True)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
