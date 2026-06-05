"""Hourly silicon system update checks.

The updater does not mutate the codebase directly. It fetches the latest Glass
release metadata, compares it with ``silicon.info``, and asks the head manager to
apply the update when the local version is behind.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
DOTENV_FILE = PROJECT_ROOT / ".env"
ENV_PY_FILE = PROJECT_ROOT / "env.py"
GLASS_CONFIG_FILE = PROJECT_ROOT / ".glass.json"
SILICON_CONFIG_FILE = PROJECT_ROOT / "silicon.json"
SILICON_INFO_FILE = PROJECT_ROOT / "silicon.info"
UPDATE_STATE_FILE = PROJECT_ROOT / "core" / "interface_state" / "system_update.json"

DEFAULT_GLASS_SERVER_URL = "https://glass.teamofsilicons.com"
UPDATE_CHECK_INTERVAL_SECONDS = 60 * 60
UPDATE_AUTH_PASSWORD = os.environ.get(
    "SILICON_UPDATE_AUTH_PASSWORD",
    "silicon-update-shared-password-v1",
)
LATEST_PATH = "/api/v1/silicon-version/latest"
AUTH_KEY_PATH = "/api/v1/silicon-version/auth-key"
REQUEST_TIMEOUT = 30


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


def _read_dotenv(path: Path = DOTENV_FILE) -> dict[str, str]:
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


def _read_env_py(path: Path = ENV_PY_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    pattern = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(['\"])(.*?)\2\s*$")
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(raw)
        if match:
            values[match.group(1)] = match.group(3)
    return values


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

    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _persist_auth_key(auth_key: str) -> None:
    if not auth_key:
        return
    _upsert_key_value(DOTENV_FILE, "SILICON_UPDATE_AUTH_KEY", auth_key)
    _upsert_key_value(DOTENV_FILE, "GLASS_API_KEY", auth_key)
    if ENV_PY_FILE.exists():
        _upsert_key_value(ENV_PY_FILE, "GLASS_API_KEY", auth_key, python_string=True)


def _glass_config() -> dict[str, Any]:
    return _read_json(GLASS_CONFIG_FILE, {})


def _silicon_config() -> dict[str, Any]:
    return _read_json(SILICON_CONFIG_FILE, {})


def _server_url() -> str:
    dotenv = _read_dotenv()
    glass = _glass_config()
    silicon = _silicon_config()
    nested_glass = silicon.get("glass") if isinstance(silicon.get("glass"), dict) else {}
    return (
        os.environ.get("GLASS_SERVER_URL")
        or dotenv.get("GLASS_SERVER_URL")
        or glass.get("server_url")
        or nested_glass.get("server_url")
        or DEFAULT_GLASS_SERVER_URL
    ).rstrip("/")


def _auth_key() -> str:
    dotenv = _read_dotenv()
    env_py = _read_env_py()
    glass = _glass_config()
    silicon = _silicon_config()
    nested_glass = silicon.get("glass") if isinstance(silicon.get("glass"), dict) else {}
    for value in (
        os.environ.get("SILICON_UPDATE_AUTH_KEY"),
        os.environ.get("GLASS_API_KEY"),
        dotenv.get("SILICON_UPDATE_AUTH_KEY"),
        dotenv.get("GLASS_API_KEY"),
        env_py.get("SILICON_UPDATE_AUTH_KEY"),
        env_py.get("GLASS_API_KEY"),
        glass.get("api_key"),
        glass.get("silicon_api_key"),
        nested_glass.get("api_key"),
        nested_glass.get("silicon_api_key"),
    ):
        if value:
            return str(value).strip()
    return ""


def _identity_payload() -> dict[str, str]:
    dotenv = _read_dotenv()
    glass = _glass_config()
    silicon = _silicon_config()
    nested_glass = silicon.get("glass") if isinstance(silicon.get("glass"), dict) else {}
    payload: dict[str, str] = {}
    candidates = {
        "silicon_id": (
            os.environ.get("SILICON_ID"),
            dotenv.get("SILICON_ID"),
            glass.get("silicon_id"),
            nested_glass.get("silicon_id"),
            silicon.get("silicon_id"),
        ),
        "silicon_username": (
            os.environ.get("SILICON_USERNAME"),
            dotenv.get("SILICON_USERNAME"),
            glass.get("silicon_username"),
            nested_glass.get("silicon_username"),
        ),
        "address": (
            os.environ.get("SILICON_ADDRESS"),
            dotenv.get("SILICON_ADDRESS"),
            glass.get("address"),
            nested_glass.get("address"),
            silicon.get("address"),
        ),
        "name": (
            os.environ.get("SILICON_NAME"),
            dotenv.get("SILICON_NAME"),
            silicon.get("name"),
        ),
    }
    for key, values in candidates.items():
        for value in values:
            if value:
                payload[key] = str(value).strip()
                break
    return payload


def _request_auth_key() -> str:
    payload = {"password": UPDATE_AUTH_PASSWORD}
    payload.update(_identity_payload())
    if not any(payload.get(k) for k in ("silicon_id", "silicon_username", "address", "name")):
        return ""

    response = requests.post(
        _server_url() + AUTH_KEY_PATH,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code not in {200, 201}:
        response.raise_for_status()
    body = response.json()
    auth_key = str(body.get("auth_key") or body.get("plaintext") or "").strip()
    _persist_auth_key(auth_key)
    return auth_key


def _fetch_latest_version() -> dict[str, Any] | None:
    auth_key = _auth_key() or _request_auth_key()
    if not auth_key:
        return None

    def do_get(key: str):
        return requests.get(
            _server_url() + LATEST_PATH,
            headers={"X-Silicon-Key": key},
            timeout=REQUEST_TIMEOUT,
        )

    response = do_get(auth_key)
    if response.status_code in {401, 403}:
        auth_key = _request_auth_key()
        if not auth_key:
            return None
        response = do_get(auth_key)

    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else None


def _local_version() -> str:
    info = _read_json(SILICON_INFO_FILE, {})
    return str(info.get("version") or "").strip() if isinstance(info, dict) else ""


def _latest_version_id(latest: dict[str, Any]) -> str:
    return str(latest.get("version_id") or latest.get("version") or "").strip()


def _head_manager_contact_id() -> str:
    from core.interface import get_contacts

    contacts = get_contacts().get("contacts", {})
    if not isinstance(contacts, dict):
        return ""
    for key, info in contacts.items():
        if not isinstance(info, dict) or not info.get("is_central_carbon"):
            continue
        return str(info.get("carbon_id") or info.get("fixed_id") or key).strip()
    return ""


def _update_message(latest: dict[str, Any], latest_version_number: str) -> str:
    update_description = str(latest.get("description") or "").strip()
    codebase_link = str(
        latest.get("codebase_url") or latest.get("codebase_link") or latest.get("download_url") or ""
    ).strip()
    return (
        "There has been a silicon system update, the updated version is: "
        f"{latest_version_number}. Once the codebase has been updated with the new version, "
        "update the version in silicon.info file with the new version, and that version number exactly! \n"
        "The basic overview of the entire update is:\n"
        f"{update_description}\n\n"
        "And the codebase is:\n"
        f"{codebase_link}\n\n"
        "Use the codebase link as your base structure and impliment it all exactly the way it's done "
        "in the codebase. Make all the required decisions, and once the update is successfully done "
        f"update the version number in silicon.info file with {latest_version_number}. \n"
    )


def check_for_system_update(now: float | None = None) -> dict[str, str]:
    """Return a manager context when a new system version is available."""
    now = time.time() if now is None else now
    state = _read_json(UPDATE_STATE_FILE, {"version": 1})
    last_checked = float(state.get("last_checked_at") or 0)
    if now - last_checked < UPDATE_CHECK_INTERVAL_SECONDS:
        return {}

    state["last_checked_at"] = now
    _write_json(UPDATE_STATE_FILE, state)

    try:
        latest = _fetch_latest_version()
    except Exception as exc:
        state["last_error"] = str(exc)
        _write_json(UPDATE_STATE_FILE, state)
        print(f"[Update] Error checking silicon version: {exc}", flush=True)
        return {}

    local_version = _local_version()
    if not latest:
        state.update({"local_version": local_version, "latest_seen_version": "", "last_error": ""})
        _write_json(UPDATE_STATE_FILE, state)
        return {}

    latest_version = _latest_version_id(latest)
    state.update({"local_version": local_version, "latest_seen_version": latest_version, "last_error": ""})

    if not latest_version or latest_version == local_version:
        state["last_notified_version"] = ""
        _write_json(UPDATE_STATE_FILE, state)
        return {}

    if state.get("last_notified_version") == latest_version:
        _write_json(UPDATE_STATE_FILE, state)
        return {}

    head_contact_id = _head_manager_contact_id()
    if not head_contact_id:
        state["last_error"] = "No central carbon contact found for system update notification."
        _write_json(UPDATE_STATE_FILE, state)
        return {}

    state["last_notified_version"] = latest_version
    _write_json(UPDATE_STATE_FILE, state)
    return {head_contact_id: _update_message(latest, latest_version)}


def trigger_system_update_check(*, force: bool = True) -> dict[str, str]:
    """Run the same update check on demand for CLI-triggered checks."""
    now = time.time() + UPDATE_CHECK_INTERVAL_SECONDS if force else None
    return check_for_system_update(now=now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trigger a silicon system update check.")
    parser.add_argument(
        "--no-force",
        action="store_true",
        help="Respect the hourly throttle instead of forcing the check.",
    )
    args = parser.parse_args(argv)
    result = trigger_system_update_check(force=not args.no_force)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
