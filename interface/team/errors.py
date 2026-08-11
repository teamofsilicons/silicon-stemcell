"""What can go wrong syncing a team, and the body-free shape it comes back as.

A failure result never carries a credential, a peer's memory, or a server
message: the caller learns that it failed and why in one word.
"""
from __future__ import annotations

from typing import Any


class TeamContextError(RuntimeError):
    """A finite synchronization or remote-contract failure."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TeamContextLockTimeout(TeamContextError):
    pass


class TeamContextIdentityChanged(TeamContextError):
    pass


def _is_authoritative_access_failure(exc: BaseException) -> bool:
    return isinstance(exc, TeamContextError) and exc.status_code in {401, 403, 404}


def _safe_failure(
    status: str,
    *,
    local_saved: bool = False,
    detail: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "status": status,
        "changed": False,
    }
    if local_saved:
        result["local_saved"] = True
    if detail:
        result["detail"] = detail
    return result
