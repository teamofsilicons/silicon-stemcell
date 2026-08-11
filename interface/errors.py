"""What can go wrong on the way to and from Interface.

Every failure that crosses back into Silicon is body-free on purpose: a
Carbon's message, a work card, or a credential must never travel inside an
exception message.
"""
from __future__ import annotations


class InterfaceError(RuntimeError):
    """Interface refused, or could not answer, a request."""


class _RPCUnavailable(RuntimeError):
    """The daemon socket was unavailable before a request could be sent."""


class _RPCUnsupported(RuntimeError):
    """The daemon rejected a command before dispatch, so CLI fallback is safe."""


class WorkCallMutationError(InterfaceError):
    """Body-free structured failure for retryable call mutations."""

    def __init__(
        self,
        *,
        status_code: int = 0,
        code: str = "",
        current_revision: int | None = None,
        retryable: bool = False,
    ):
        self.status_code = int(status_code or 0)
        self.code = str(code or "")[:80]
        self.current_revision = current_revision
        self.retryable = bool(retryable)
        suffix = f" HTTP {self.status_code}" if self.status_code else ""
        super().__init__(f"Work call mutation failed{suffix}.")


class CallBookkeepingError(InterfaceError):
    """A body-free signal that a durable call intent was not committed."""


class DurableHandoffError(InterfaceError):
    """A body-free signal that manager-root ownership was not confirmed."""


