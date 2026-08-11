"""Handing a Carbon a live view of the browser, and taking a message back.

Both are control-plane operations: Interface owns the session and the event,
Stemcell owns the per-contact lock that stops two shares racing for one
profile.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from helpers.paths import STATE_DIR
from helpers.state import file_lock, update_json
from interface import client as client_module
from interface import constants
from interface.constants import (
    PROJECT_ROOT,
    REMOTE_BROWSER_START_URL,
    URL_RE,
)
from interface import outbound as outbound_module


def parse_remote_browser_url(stdout: str) -> str:
    match = URL_RE.search(stdout or "")
    return match.group(0).rstrip(".,)") if match else ""


def _normalize_remote_browser_start_url(value: str | None) -> str:
    url = (value or "").strip() or REMOTE_BROWSER_START_URL
    if not url:
        return "https://www.google.com"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = f"https://{url}"
    if not url.lower().startswith(("http://", "https://")):
        return REMOTE_BROWSER_START_URL
    return url


def _remote_browser_cmd(session_name: str, profile: str, *parts: str) -> list[str]:
    return [
        "silicon-browser",
        "--session",
        session_name,
        "--profile",
        profile,
        *parts,
    ]


def _remote_browser_output(proc: subprocess.CompletedProcess[str]) -> str:
    return ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()


def _share_missing_session(output: str) -> bool:
    text = output.lower()
    return "no active session" in text or "open a page first" in text


# Maps an active share session ("remote-<contact>") to the interface event_id
# of its card, so `close` can tell the interface to grey that card out.


def _extract_event_id(posted: Any) -> str:
    if isinstance(posted, dict):
        ev = posted.get("event") if isinstance(posted.get("event"), dict) else posted
        eid = ev.get("event_id") or ev.get("id")
        if isinstance(eid, str):
            return eid
    return ""


def _extract_remote_browser_url(posted: Any, fallback: str = "") -> str:
    if isinstance(posted, dict):
        ev = posted.get("event") if isinstance(posted.get("event"), dict) else posted
        content = ev.get("content") if isinstance(ev.get("content"), dict) else {}
        url = content.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return fallback


def _save_remote_browser_event(session_name: str, event_id: str) -> None:
    try:
        def remember(state):
            if isinstance(state, dict):
                state[session_name] = event_id

        update_json(constants.REMOTE_BROWSER_STATE_FILE, {}, remember)
    except Exception:
        pass


def _pop_remote_browser_event(session_name: str) -> str:
    event_id = ""
    try:
        def pop_event(state):
            nonlocal event_id
            if isinstance(state, dict):
                event_id = state.pop(session_name, "")

        update_json(constants.REMOTE_BROWSER_STATE_FILE, {}, pop_event)
    except Exception:
        return ""
    return event_id if isinstance(event_id, str) else ""


def _remote_browser_lock_path(contact_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(contact_id or "contact"))[:80]
    return STATE_DIR / f"remote-browser-{safe_id}.json"


def remote_browser_share(contact_id: str, expiry: int = 60, new: bool = True, url: str = "") -> str:
    with file_lock(_remote_browser_lock_path(contact_id)):
        return _remote_browser_share_locked(contact_id, expiry=expiry, new=new, url=url)


def _remote_browser_share_locked(contact_id: str, expiry: int = 60, new: bool = True, url: str = "") -> str:
    contact, err = outbound_module._contact_room_or_error(contact_id)
    if err:
        return err
    assert contact is not None

    from worker.handler import SILICON_BROWSER_PROFILE

    try:
        minutes = int(expiry or 60)
    except (TypeError, ValueError):
        minutes = 60
    if minutes <= 0:
        minutes = 60

    session_name = f"remote-{contact_id}"
    start_url = _normalize_remote_browser_start_url(url)

    def open_session() -> str:
        open_cmd = _remote_browser_cmd(
            session_name,
            SILICON_BROWSER_PROFILE,
            "open",
            start_url,
            "--timeout",
            str(minutes),
        )
        open_proc = subprocess.run(open_cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=180)
        if open_proc.returncode != 0:
            return _remote_browser_output(open_proc)
        return ""

    if new:
        close_cmd = _remote_browser_cmd(session_name, SILICON_BROWSER_PROFILE, "close")
        subprocess.run(close_cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
        open_error = open_session()
        if open_error:
            return f"Error: silicon-browser open failed: {open_error}"

    cmd = _remote_browser_cmd(
        session_name,
        SILICON_BROWSER_PROFILE,
        "share",
        "--expiry",
        str(minutes),
    )
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120)
    output = _remote_browser_output(proc)
    if proc.returncode != 0 and not new and _share_missing_session(output):
        open_error = open_session()
        if open_error:
            return f"Error: silicon-browser open failed: {open_error}"
        proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120)
        output = _remote_browser_output(proc)
    if proc.returncode != 0:
        return f"Error: silicon-browser share failed: {output}"
    url = parse_remote_browser_url(output)
    if not url:
        return f"Error: silicon-browser did not return a share URL: {output}"

    posted = client_module.InterfaceClient().remote_browser(contact["room_id"], url, minutes)
    event_id = _extract_event_id(posted)
    if event_id:
        _save_remote_browser_event(session_name, event_id)
    branded_url = _extract_remote_browser_url(posted, fallback=url)
    return f"Done. Remote browser shared. session={session_name}, expiry_minutes={minutes}, url={branded_url}"


def remote_browser_close(contact_id: str) -> str:
    with file_lock(_remote_browser_lock_path(contact_id)):
        return _remote_browser_close_locked(contact_id)


def _remote_browser_close_locked(contact_id: str) -> str:
    from worker.handler import SILICON_BROWSER_PROFILE

    session_name = f"remote-{contact_id}"
    cmd = [
        "silicon-browser",
        "--session",
        session_name,
        "--profile",
        SILICON_BROWSER_PROFILE,
        "close",
    ]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"Error: silicon-browser close failed: {output}"

    # Tell the interface the card is closed so it greys out immediately,
    # rather than counting down to its original expiry. Best-effort.
    event_id = _pop_remote_browser_event(session_name)
    if event_id:
        try:
            from interface.config import silicon_api_post

            silicon_api_post(f"/api/v1/events/{event_id}/remote_browser_close")
        except Exception as exc:  # noqa: BLE001 — close must not fail on the card update
            return (
                f"Done. Remote browser closed. session={session_name}. Profile state saved. "
                f"(card update skipped: {exc})"
            )
    return f"Done. Remote browser closed. session={session_name}. Profile state saved."


def complete_take_back(request_id: str, replacement: str) -> str:
    if not request_id:
        return "Error: request_id is required"
    payload = client_module.InterfaceClient().take_back_complete(request_id, replacement or "")
    return "Done. Take-back completed." + (f" {json.dumps(payload)}" if payload else "")


def take_back_event(event_id: str, reason: str = "", force: bool = False) -> str:
    if not event_id:
        return "Error: event_id is required"
    payload = client_module.InterfaceClient().take_back_event(event_id, reason=reason, force=force)
    return "Done. Event take-back requested." + (f" {json.dumps(payload)}" if payload else "")
