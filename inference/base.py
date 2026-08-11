"""The contract a provider signs.

Silicon reaches a model two ways: a synchronous streaming turn it waits on (a
manager or the advisor thinking), and a detached process it launches and reads
later (a worker doing real work). A provider must serve both, and must be able
to read its own output back — that is the whole of what the rest of the system
knows about it.

Everything above this line is provider-agnostic. If a caller outside
``inference/`` ever branches on a provider name, this contract is missing a
method.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from inference.models import (
    TurnRequest,
    TurnResult,
    WorkerCommand,
    WorkerLaunchSpec,
    WorkerOutcome,
)
from inference.sessions import SessionStore


class InferenceProvider(ABC):
    """One way of thinking. Registered by :data:`name`."""

    #: The name used in ``silicon.json`` and in worker records.
    name: ClassVar[str] = ""

    #: True when the provider's server mints session ids, so Silicon cannot
    #: invent one and a resume without a stored id is impossible.
    mints_own_session_id: ClassVar[bool] = False

    def __init__(self) -> None:
        self.sessions = SessionStore(self.name)

    # -- conversations ----------------------------------------------------

    @abstractmethod
    def new_session(self, session_key: str) -> str:
        """Reset this key's conversation. Returns a description of the result."""

    # -- synchronous turns (manager, advisor) -----------------------------

    @abstractmethod
    def run_turn(self, request: TurnRequest) -> TurnResult:
        """Run one turn to completion, streaming progress as it goes.

        Raises :class:`~inference.errors.ProviderTimeoutError` when the turn
        passes its deadline. Every other failure comes back as a ``TurnResult``
        whose output is a Carbon-safe tools payload.
        """

    # -- detached runs (workers) ------------------------------------------

    @abstractmethod
    def worker_command(self, spec: WorkerLaunchSpec) -> WorkerCommand:
        """Build the argv and stdin mode for a detached worker run."""

    @abstractmethod
    def read_output(self, raw: str) -> WorkerOutcome:
        """Interpret a worker's raw output file: result, state, session id."""

    @abstractmethod
    def progress_events(self, event: dict, state: dict) -> list[dict]:
        """Normalize one raw provider event into Silicon's progress vocabulary.

        ``state`` is carried across a whole stream so a provider can correlate
        an event with the ones before it.
        """

    def session_id_from_output(self, raw: str) -> str:
        """The session id a provider only reveals in its own output stream."""
        return self.read_output(raw).session_id

    def has_completion_event(self, raw: str) -> bool:
        """Whether the output already contains this provider's end-of-turn event."""
        return self.read_output(raw).completed

    def terminal_state(self, raw: str) -> str:
        """``"completed"`` or ``"failed"``, from the provider's own verdict."""
        return self.read_output(raw).state

    def parse_output(self, raw: str) -> str:
        """The human-readable result to hand back to a manager."""
        return self.read_output(raw).result
