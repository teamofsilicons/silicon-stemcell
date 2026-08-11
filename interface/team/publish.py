"""The manager-facing write path for own advertising memory.
"""
from __future__ import annotations

from interface.team import drafts as drafts_module
from interface.team import errors as errors_module
from interface.team import http as http_module
from interface.team import identity as identity_module
from interface.team import locks as locks_module
from interface.team import memory as memory_module
from interface.team import own_upload as own_upload_module
from interface.team import paths as paths_module
from interface.team import service as service_module
from interface.team import state as state_module
from pathlib import Path
from typing import Any


def update_own_advertising_memory(
    content: str,
    root: str | Path | None = None,
    *,
    resolve_conflict: bool = False,
) -> dict[str, Any]:
    """Save and CAS-upload this authenticated Silicon's own advertising memory.

    The caller cannot supply a path or Silicon ID. Identity comes from Glass,
    with the last server-verified cached identity used only to preserve a local
    draft while Glass is temporarily unavailable. A conflict or failed upload
    never discards the atomically written local content. Normal calls preserve
    an existing conflict without retrying stale CAS state; pass
    ``resolve_conflict=True`` to intentionally replace the latest remote
    revision with this content.
    """

    if not isinstance(resolve_conflict, bool):
        return errors_module._safe_failure(
            "invalid",
            detail="resolve_conflict must be a boolean.",
        )
    try:
        memory_module.validate_advertising_memory(content)
    except ValueError as exc:
        return errors_module._safe_failure("invalid", detail=str(exc))

    project_root = paths_module._normalise_root(root)
    try:
        try:
            paths_module.ensure_team_context_layout(project_root)
        except errors_module.TeamContextError as exc:
            return errors_module._safe_failure("invalid", detail=str(exc))
        with locks_module._sync_lock(project_root):
            state = state_module._load_state(project_root)
            previous_identity = identity_module._stored_identity(state)
            identity: dict[str, Any] | None = None
            config: dict[str, Any] | None = None
            server_origin = ""
            identity_error = False
            result: dict[str, Any] | None = None
            local_saved = False
            try:
                config, server_origin = http_module._load_config_snapshot(project_root)
            except Exception:
                identity_error = True
                identity = identity_module._cached_identity(state)
            else:
                try:
                    identity_module._mark_configured_scope_change(
                        project_root,
                        state,
                        server_origin,
                        http_module._credential_fingerprint(config),
                    )
                    identity = identity_module._fetch_identity(
                        project_root,
                        config=config,
                        server_origin=server_origin,
                    )
                except Exception as exc:
                    identity_error = True
                    if errors_module._is_authoritative_access_failure(exc):
                        identity_module._invalidate_team_access(project_root, state)
                        archive_identity = (
                            previous_identity
                            if previous_identity is not None
                            and previous_identity["server_origin"] == server_origin
                            else None
                        )
                        try:
                            drafts_module._archive_explicit_draft(
                                project_root,
                                state,
                                content,
                                identity=archive_identity,
                                server_origin=server_origin,
                            )
                            local_saved = True
                        except Exception:
                            local_saved = False
                        result = errors_module._safe_failure(
                            "unauthorized",
                            local_saved=local_saved,
                        )
                    else:
                        cached = identity_module._cached_identity(state)
                        if (
                            cached is not None
                            and cached["server_origin"] == server_origin
                            and cached["credential_fingerprint"]
                            == http_module._credential_fingerprint(config)
                        ):
                            identity = cached
                        else:
                            result = errors_module._safe_failure("identity_unavailable")
                else:
                    try:
                        identity_module._transition_identity(project_root, state, identity)
                    except Exception:
                        identity = None
                        result = errors_module._safe_failure("state_error")

            if identity is None and result is None:
                result = errors_module._safe_failure("identity_unavailable")

            if identity is None and not local_saved:
                archive_identity = (
                    previous_identity
                    if previous_identity is not None
                    and (
                        not server_origin
                        or previous_identity["server_origin"] == server_origin
                    )
                    else None
                )
                try:
                    drafts_module._archive_explicit_draft(
                        project_root,
                        state,
                        content,
                        identity=archive_identity,
                        server_origin=server_origin,
                    )
                    local_saved = True
                except Exception:
                    local_saved = False
                if result is None:
                    result = errors_module._safe_failure("identity_unavailable")
                result["local_saved"] = local_saved

            if identity is not None:
                own_path = paths_module._advertising_file(
                    project_root,
                    identity["silicon_id"],
                )
                paths_module._atomic_write_bytes(
                    project_root,
                    own_path,
                    content.encode("utf-8"),
                )
                local_saved = True

                if identity_error:
                    result = errors_module._safe_failure("pending", local_saved=True)
                    own = state.setdefault("own", {})
                    own["silicon_id"] = identity["silicon_id"]
                    own["status"] = "pending"
                    own["pending_sha256"] = paths_module._sha256(content.encode("utf-8"))
                else:
                    try:
                        result = own_upload_module._upload_explicit_own(
                            project_root,
                            state,
                            identity,
                            content,
                            resolve_conflict=bool(resolve_conflict),
                            config=config,
                        )
                    except errors_module.TeamContextIdentityChanged:
                        result = errors_module._safe_failure(
                            "identity_changed",
                            local_saved=True,
                        )
                    except Exception as exc:
                        if errors_module._is_authoritative_access_failure(exc):
                            identity_module._invalidate_team_access(project_root, state)
                            result = errors_module._safe_failure(
                                "unauthorized",
                                local_saved=True,
                            )
                        else:
                            result = errors_module._safe_failure(
                                "pending",
                                local_saved=True,
                            )
                            if identity_module._cached_identity(state) == identity:
                                own = state.setdefault("own", {})
                                if own.get("status") != "conflict":
                                    own["silicon_id"] = identity["silicon_id"]
                                    own["status"] = "pending"
                                    own["pending_sha256"] = paths_module._sha256(
                                        content.encode("utf-8")
                                    )
            try:
                state_module._save_state(project_root, state)
            except Exception:
                return errors_module._safe_failure("state_error", local_saved=local_saved)
            return result or errors_module._safe_failure("unavailable", local_saved=local_saved)
    except errors_module.TeamContextLockTimeout:
        return errors_module._safe_failure("busy")
    except Exception:
        service_module.log.exception("own advertising-memory update failed")
        return errors_module._safe_failure("unavailable")
