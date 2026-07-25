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

from core.glass import silicon_api_request
from core.runtime_paths import DATA_ROOT
from core.state_store import read_json, write_json

STATE_FILE = "core/interface_state/trust_policy.json"
VALID_LEVELS = ("very_low", "low", "ok", "high", "very_high", "ultimate")
VALID_KINDS = ("carbon", "silicon")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


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
    }


def _load_state(root: Path) -> dict[str, Any]:
    raw = read_json(_state_path(root), _default_state())
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return _default_state()
    state = _default_state()
    state.update(raw)
    if not isinstance(state.get("entries"), dict):
        state["entries"] = {}
    return state


def _save_state(root: Path, state: dict[str, Any]) -> None:
    state["version"] = 1
    write_json(_state_path(root), state)


def _key(kind: str, public_id: str) -> str:
    return f"{kind}:{public_id}"


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
        if (
            kind not in VALID_KINDS
            or not ID_RE.fullmatch(public_id)
            or level not in VALID_LEVELS
        ):
            raise TrustSyncError("Glass returned an invalid typed trust target.")
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
            "level": level,
            "source": str(raw.get("effective_source") or ""),
            "base_level": raw.get("base_level"),
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


def _contacts_for_bootstrap(root: Path) -> list[dict[str, str]]:
    from core.interface import get_contacts

    state = get_contacts()
    contacts = state.get("contacts", {}) if isinstance(state, dict) else {}
    out = []
    for fixed_id, contact in contacts.items():
        if not isinstance(contact, dict):
            continue
        kind = str(contact.get("contact_type") or "")
        level = str(contact.get("trust_level") or "")
        if (
            kind in VALID_KINDS
            and ID_RE.fullmatch(str(fixed_id))
            and level in VALID_LEVELS
        ):
            out.append({"kind": kind, "id": str(fixed_id), "level": level})
    return out


def _apply_confirmed_policy(
    root: Path,
    payload: dict,
    *,
    etag: str = "",
    bootstrapped: bool = True,
) -> dict[str, Any]:
    validated = _validate_policy(payload)
    from core.interface import apply_glass_trust_policy

    changed = apply_glass_trust_policy(validated["entries"])
    state = _load_state(root)
    state.update(validated)
    state["etag"] = str(etag or "")
    state["server_bootstrapped"] = bool(bootstrapped)
    state["last_confirmed_at"] = time.time()
    state["last_error"] = ""
    _save_state(root, state)
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
                json_body={"contacts": _contacts_for_bootstrap(project_root)},
                timeout=30,
            )
            if bootstrap.status_code != 200:
                raise TrustSyncError("Glass did not accept the local trust bootstrap.")
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
            # The full confirmed snapshot is retained locally, so a prior crash
            # between cache and contacts application can still self-repair.
            from core.interface import apply_glass_trust_policy

            changed = apply_glass_trust_policy(state.get("entries", {}))
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
        state = _load_state(project_root)
        state["last_error"] = str(exc)[:300]
        _save_state(project_root, state)
        return {
            "status": "deferred",
            "reason": reason,
            "error": str(exc)[:300],
            "revision": state.get("revision", "0:0"),
        }


def cached_trust_level(
    kind: str,
    public_id: str,
    *,
    root: str | Path | None = None,
) -> str:
    """Return only a last-confirmed Glass value; unknown identities fail closed."""
    if kind not in VALID_KINDS or not ID_RE.fullmatch(str(public_id or "")):
        return "very_low"
    entry = _load_state(_root(root)).get("entries", {}).get(_key(kind, public_id))
    if not isinstance(entry, dict):
        return "very_low"
    level = str(entry.get("level") or "")
    return level if level in VALID_LEVELS else "very_low"


def has_confirmed_policy(*, root: str | Path | None = None) -> bool:
    state = _load_state(_root(root))
    return bool(
        state.get("server_bootstrapped")
        and float(state.get("last_confirmed_at") or 0) > 0
    )


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
