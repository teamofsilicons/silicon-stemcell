"""Glass-first trust policy cache and local mutation transport.

Glass is authoritative. This module mirrors a revisioned effective policy into
Stemcell and updates ``contacts.json`` only after Glass confirms a revision.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from interface.config import silicon_api_request
from helpers.paths import DATA_ROOT
from helpers.state import file_lock, read_json, write_json

STATE_FILE = "interface/state/trust_policy.json"
VALID_LEVELS = ("very_low", "low", "ok", "high", "very_high", "ultimate")
VALID_KINDS = ("carbon", "silicon")
VALID_SOURCES = ("central_carbon", "silicon_override", "team_base", "default")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
MAX_POLICY_ENTRIES = 10_000
MAX_TARGET_NAME_BYTES = 512


class TrustSyncError(RuntimeError):
    pass


def _root(root: str | Path | None = None) -> Path:
    return Path(root or DATA_ROOT).resolve()


def _state_path(root: Path) -> Path:
    return root / STATE_FILE


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "etag": "",
        "source_silicon_id": "",
        "team_revision": 0,
        "silicon_revision": 0,
        "revision": "0:0",
        "server_bootstrapped": False,
        "entries": {},
        "last_confirmed_at": 0,
        "last_error": "",
        "pending_team_revision": 0,
        "pending_silicon_revision": 0,
        "invalidated_at": 0,
    }


def _normalise_state(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return _default_state()
    state = _default_state()
    state.update(raw)
    if not isinstance(state.get("entries"), dict):
        state["entries"] = {}
    return state


def _load_state(root: Path) -> dict[str, Any]:
    return _normalise_state(read_json(_state_path(root), _default_state()))


def _save_state(root: Path, state: dict[str, Any]) -> None:
    state["version"] = 1
    write_json(_state_path(root), state)


def _update_state(root: Path, update) -> dict[str, Any]:
    """Serialize a state transition across the run process and Glass sidecar."""

    path = _state_path(root)
    with file_lock(path):
        state = _load_state(root)
        update(state)
        _save_state(root, state)
        return state


def _key(kind: str, public_id: str) -> str:
    return f"{kind}:{public_id}"


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _nonnegative_float(value: object) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _one_line_name(value: object) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TARGET_NAME_BYTES:
        return text
    return encoded[:MAX_TARGET_NAME_BYTES].decode("utf-8", errors="ignore").rstrip() + "…"


def _validate_policy(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TrustSyncError("Glass returned an invalid trust policy.")
    source = payload.get("source_silicon")
    if not isinstance(source, dict) or not ID_RE.fullmatch(str(source.get("id") or "")):
        raise TrustSyncError("Glass returned an invalid source Silicon identity.")
    try:
        team_revision = int(payload.get("team_revision"))
        silicon_revision = int(payload.get("silicon_revision"))
    except (TypeError, ValueError) as exc:
        raise TrustSyncError("Glass returned invalid trust revisions.") from exc
    if team_revision < 0 or silicon_revision < 0:
        raise TrustSyncError("Glass returned invalid trust revisions.")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise TrustSyncError("Glass returned invalid trust entries.")
    if len(raw_entries) > MAX_POLICY_ENTRIES:
        raise TrustSyncError("Glass returned too many trust entries.")

    entries: dict[str, dict[str, Any]] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise TrustSyncError("Glass returned an invalid trust entry.")
        target = raw.get("target")
        if not isinstance(target, dict):
            raise TrustSyncError("Glass returned an invalid typed trust target.")
        kind = str(target.get("kind") or "")
        public_id = str(target.get("id") or "")
        level = str(raw.get("effective_level") or "")
        source_label = str(raw.get("effective_source") or "")
        if (
            kind not in VALID_KINDS
            or not ID_RE.fullmatch(public_id)
            or level not in VALID_LEVELS
            or source_label not in VALID_SOURCES
        ):
            raise TrustSyncError("Glass returned an invalid typed trust target.")
        base_level = raw.get("base_level")
        if base_level is not None and base_level not in VALID_LEVELS:
            raise TrustSyncError("Glass returned an invalid base trust value.")
        override_level = raw.get("override_level")
        if override_level is not None and override_level not in VALID_LEVELS:
            raise TrustSyncError("Glass returned an invalid trust override.")
        try:
            override_revision = int(raw.get("override_revision") or 0)
        except (TypeError, ValueError) as exc:
            raise TrustSyncError("Glass returned an invalid override revision.") from exc
        entries[_key(kind, public_id)] = {
            "kind": kind,
            "id": public_id,
            "name": _one_line_name(target.get("name") or public_id),
            "level": level,
            "source": source_label,
            "base_level": base_level,
            "override_level": override_level,
            "override_revision": max(0, override_revision),
            "central_carbon": bool(raw.get("central_carbon")),
        }
    return {
        "source_silicon_id": str(source["id"]),
        "team_revision": team_revision,
        "silicon_revision": silicon_revision,
        "revision": f"{team_revision}:{silicon_revision}",
        "entries": entries,
    }


def _pending_revision_is_newer(state: dict[str, Any]) -> bool:
    return (
        _nonnegative_int(state.get("pending_team_revision"))
        > _nonnegative_int(state.get("team_revision"))
        or _nonnegative_int(state.get("pending_silicon_revision"))
        > _nonnegative_int(state.get("silicon_revision"))
    )


def mark_trust_policy_invalidated(
    *,
    team_revision: object = 0,
    silicon_revision: object = 0,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Record a Glass revision nudge before scheduling its HTTP reconciliation.

    The last confirmed revision remains active while the newer advertised
    revision is fetched, validated, and applied.
    """

    project_root = _root(root)
    try:
        team_value = max(0, int(team_revision or 0))
        silicon_value = max(0, int(silicon_revision or 0))
    except (TypeError, ValueError):
        team_value = 0
        silicon_value = 0

    def update(state: dict[str, Any]) -> None:
        state["pending_team_revision"] = max(
            _nonnegative_int(state.get("pending_team_revision")),
            team_value,
        )
        state["pending_silicon_revision"] = max(
            _nonnegative_int(state.get("pending_silicon_revision")),
            silicon_value,
        )
        state["invalidated_at"] = time.time()

    return _update_state(project_root, update)


def _apply_confirmed_policy(
    root: Path,
    payload: dict,
    *,
    etag: str = "",
    bootstrapped: bool = True,
) -> dict[str, Any]:
    validated = _validate_policy(payload)
    from interface.adapter import apply_glass_trust_policy

    changed = apply_glass_trust_policy(validated["entries"])

    def update(state: dict[str, Any]) -> None:
        state.update(validated)
        state["etag"] = str(etag or "")
        state["server_bootstrapped"] = bool(bootstrapped)
        state["last_confirmed_at"] = time.time()
        state["last_error"] = ""
        if not _pending_revision_is_newer(state):
            state["pending_team_revision"] = 0
            state["pending_silicon_revision"] = 0
            state["invalidated_at"] = 0

    _update_state(root, update)
    return {"changed_contacts": changed, **validated}


def _ack(root: Path, policy: dict[str, Any]) -> None:
    response = silicon_api_request(
        "POST",
        "/api/v1/silicons/me/trust-ack",
        start=root,
        json_body={
            "team_revision": policy["team_revision"],
            "silicon_revision": policy["silicon_revision"],
        },
        timeout=15,
    )
    if response.status_code != 200:
        raise TrustSyncError("Glass did not accept the trust-policy acknowledgement.")


def reconcile_trust_policy(
    root: str | Path | None = None,
    *,
    force: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Fetch, validate, apply, persist, and acknowledge Glass trust policy."""
    project_root = _root(root)
    state = _load_state(project_root)
    try:
        if not state.get("server_bootstrapped"):
            bootstrap = silicon_api_request(
                "POST",
                "/api/v1/silicons/me/trust-bootstrap",
                start=project_root,
                # New Stemcells never seed authority from editable local state.
                # The endpoint retains legacy import support for old clients,
                # but Glass is the sole source for this runtime.
                json_body={"contacts": []},
                timeout=30,
            )
            if bootstrap.status_code != 200:
                raise TrustSyncError("Glass did not accept the trust bootstrap.")
            body = bootstrap.json() or {}
            result = _apply_confirmed_policy(
                project_root,
                body.get("policy"),
                etag="",
                bootstrapped=True,
            )
            _ack(project_root, result)
            return {"status": "updated", "reason": reason, **result}

        headers = {}
        if state.get("etag") and not force:
            headers["If-None-Match"] = state["etag"]
        response = silicon_api_request(
            "GET",
            "/api/v1/silicons/me/trust-policy",
            start=project_root,
            headers=headers,
            timeout=20,
        )
        if response.status_code == 304:
            state = _load_state(project_root)
            if _pending_revision_is_newer(state):
                raise TrustSyncError(
                    "Glass returned an unchanged policy while a newer revision is pending."
                )
            # The full confirmed snapshot is retained locally, so a prior crash
            # between cache and contacts application can still self-repair.
            from interface.adapter import apply_glass_trust_policy

            changed = apply_glass_trust_policy(state.get("entries", {}))
            _update_state(
                project_root,
                lambda current: current.update(
                    last_confirmed_at=time.time(),
                    last_error="",
                ),
            )
            _ack(project_root, state)
            return {
                "status": "unchanged",
                "reason": reason,
                "changed_contacts": changed,
                "revision": state.get("revision", "0:0"),
            }
        if response.status_code != 200:
            raise TrustSyncError("Glass trust policy is temporarily unavailable.")
        result = _apply_confirmed_policy(
            project_root,
            response.json(),
            etag=response.headers.get("ETag", ""),
        )
        _ack(project_root, result)
        return {"status": "updated", "reason": reason, **result}
    except Exception as exc:
        error_text = str(exc)[:300]
        state = _update_state(
            project_root,
            lambda current: current.update(last_error=error_text),
        )
        return {
            "status": "deferred",
            "reason": reason,
            "error": error_text,
            "revision": state.get("revision", "0:0"),
        }


def cached_trust_level(
    kind: str,
    public_id: str,
    *,
    root: str | Path | None = None,
) -> str:
    """Return only a last-confirmed Glass value; unknown identities fail closed."""
    entry = cached_trust_entry(kind, public_id, root=root)
    level = str(entry.get("level") or "")
    return level if level in VALID_LEVELS else "very_low"


def cached_trust_entry(
    kind: str,
    public_id: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Return the last confirmed Glass projection; unknown values fail closed."""

    public_id = str(public_id or "")
    if kind not in VALID_KINDS or not ID_RE.fullmatch(public_id):
        return {}
    state = _load_state(_root(root))
    if not _state_has_confirmed_policy(state):
        return {}
    return _cached_entry_from_state(state, kind, public_id)


def _cached_entry_from_state(
    state: dict[str, Any],
    kind: str,
    public_id: str,
) -> dict[str, Any]:
    entry = state.get("entries", {}).get(_key(kind, public_id))
    if not isinstance(entry, dict):
        return {}
    level = str(entry.get("level") or "")
    source = str(entry.get("source") or "")
    if level not in VALID_LEVELS or source not in VALID_SOURCES:
        return {}
    return {
        "kind": kind,
        "id": public_id,
        "name": _one_line_name(entry.get("name") or public_id),
        "level": level,
        "source": source,
        "base_level": (
            entry.get("base_level")
            if entry.get("base_level") in VALID_LEVELS
            else None
        ),
        "override_level": (
            entry.get("override_level")
            if entry.get("override_level") in VALID_LEVELS
            else None
        ),
        "override_revision": _nonnegative_int(entry.get("override_revision")),
        "central_carbon": bool(entry.get("central_carbon")),
    }


def _state_has_confirmed_policy(state: dict[str, Any]) -> bool:
    confirmed_at = _nonnegative_float(state.get("last_confirmed_at"))
    return bool(
        state.get("server_bootstrapped")
        and confirmed_at > 0
    )


def _state_has_current_policy(state: dict[str, Any]) -> bool:
    return bool(
        _state_has_confirmed_policy(state)
        and not _pending_revision_is_newer(state)
    )


def confirmed_trust_policy_snapshot(
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a prompt/tool-safe view of the current Glass-confirmed policy."""

    project_root = _root(root)
    state = _load_state(project_root)
    confirmed = _state_has_confirmed_policy(state)
    current = _state_has_current_policy(state)
    entries = []
    if confirmed:
        for key in sorted(state.get("entries", {})):
            raw = state["entries"].get(key)
            if not isinstance(raw, dict):
                continue
            entry = _cached_entry_from_state(
                state,
                str(raw.get("kind") or ""),
                str(raw.get("id") or ""),
            )
            if entry:
                entries.append(entry)
    return {
        "status": (
            "current"
            if current
            else "refresh_pending"
            if _pending_revision_is_newer(state)
            else "unavailable"
        ),
        "source_silicon_id": str(state.get("source_silicon_id") or ""),
        "team_revision": _nonnegative_int(state.get("team_revision")),
        "silicon_revision": _nonnegative_int(state.get("silicon_revision")),
        "revision": str(state.get("revision") or "0:0"),
        "last_confirmed_at": _nonnegative_float(state.get("last_confirmed_at")),
        "entries": entries,
    }


def inspect_trust_policy(
    *,
    kind: str = "",
    public_id: str = "",
    root: str | Path | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Read the canonical projection, optionally refreshing it from Glass first."""

    project_root = _root(root)
    refresh_result = None
    if refresh:
        refresh_result = reconcile_trust_policy(
            project_root,
            force=True,
            reason="manager-inspection",
        )
    snapshot = confirmed_trust_policy_snapshot(root=project_root)
    if kind or public_id:
        if kind not in VALID_KINDS or not ID_RE.fullmatch(str(public_id or "")):
            raise ValueError("A valid typed trust target is required.")
        snapshot["entries"] = [
            entry
            for entry in snapshot["entries"]
            if entry["kind"] == kind and entry["id"] == public_id
        ]
    if refresh_result is not None:
        snapshot["refresh_status"] = refresh_result.get("status", "")
        if refresh_result.get("error"):
            snapshot["refresh_error"] = str(refresh_result["error"])[:300]
    return snapshot


def set_contact_trust(
    kind: str,
    public_id: str,
    level: str | None,
    *,
    reason: str = "",
    initiated_by_carbon_id: str = "",
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Commit a local Silicon decision to Glass, then mirror its canonical result."""
    project_root = _root(root)
    if kind not in VALID_KINDS:
        raise ValueError("kind must be carbon or silicon")
    if not ID_RE.fullmatch(str(public_id or "")):
        raise ValueError("invalid target identity")
    if level is not None and level not in VALID_LEVELS:
        raise ValueError("invalid trust level")
    if not _state_has_current_policy(_load_state(project_root)):
        refreshed = reconcile_trust_policy(
            project_root,
            force=True,
            reason="before-local-mutation",
        )
        if (
            refreshed.get("status") == "deferred"
            or not _state_has_current_policy(_load_state(project_root))
        ):
            raise TrustSyncError(
                "Glass trust policy is not current; no trust change was made."
            )
    state = _load_state(project_root)
    entry = state.get("entries", {}).get(_key(kind, public_id), {})
    expected_revision = (
        int(entry.get("override_revision") or 0)
        if isinstance(entry, dict)
        else 0
    )
    response = silicon_api_request(
        "PATCH",
        "/api/v1/silicons/me/trust-overrides",
        start=project_root,
        json_body={
            "target_kind": kind,
            "target_id": public_id,
            "level": level,
            "expected_revision": expected_revision,
            "reason": str(reason or "")[:500],
            "initiated_by_carbon_id": str(initiated_by_carbon_id or ""),
            "operation_id": uuid.uuid4().hex,
        },
        timeout=20,
    )
    body = response.json() if response.content else {}
    if response.status_code == 409:
        policy = body.get("policy") if isinstance(body, dict) else None
        if isinstance(policy, dict):
            _apply_confirmed_policy(project_root, policy)
        raise TrustSyncError(
            str(body.get("detail") or "Trust changed in Glass; refresh and retry.")
        )
    if response.status_code != 200:
        raise TrustSyncError(
            str(body.get("detail") or "Glass did not accept the trust change.")
        )
    result = _apply_confirmed_policy(project_root, body)
    _ack(project_root, result)
    return {
        "status": "updated",
        "target": _key(kind, public_id),
        "level": cached_trust_level(kind, public_id, root=project_root),
        "revision": result["revision"],
    }
