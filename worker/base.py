"""What every worker is, and the four places the three kinds differ.

A worker is one detached run against a provider, owned by one contact, with a
durable record that outlives it. Starting, messaging, stopping, and reporting
are the same for all three kinds; what differs is small enough to name:

    browser   a shared profile that only one worker may hold, so a second one
              queues; its own environment, model, and daemon to clean up
    terminal  must already be registered, and forgets itself if the launch fails
    writer    nothing — writer is the base behaviour

A subclass registers itself by declaring ``worker_type``; nothing here imports
a subclass, which is what keeps the dispatch one-directional.
"""
from __future__ import annotations

from abc import ABC
from typing import ClassVar

from helpers.state import file_lock
from worker.process import (
    _launch_with_provider_order,
    _launch_worker_process,
    _normalize_provider,
)
from worker.registry import (
    _get_worker_record,
    _load_active,
    _worker_launch_lock_path,
)

_REGISTRY: dict[str, type["Worker"]] = {}


class Worker(ABC):
    """One worker, of whichever kind its ``worker_type`` names."""

    #: The name a manager uses, and the key this class is registered under.
    worker_type: ClassVar[str] = ""

    #: Terminal workers must already exist; the others are created on demand.
    requires_existing_record: ClassVar[bool] = False

    #: Whether an "already active" refusal should also drop the durable record.
    #: Terminal deletes its own record on launch failure instead.
    forget_record_on_start_error: ClassVar[bool] = True

    def __init__(self, worker_id: str, carbon_id: str) -> None:
        self.worker_id = worker_id
        self.carbon_id = carbon_id

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if cls.worker_type:
            _REGISTRY[cls.worker_type] = cls

    # -- resolution -------------------------------------------------------

    @staticmethod
    def kinds() -> list[str]:
        return sorted(_REGISTRY)

    @classmethod
    def resolve(cls, worker_type: str, worker_id: str, carbon_id: str) -> "Worker | None":
        """The worker of that kind, or None if there is no such kind."""
        subclass = _REGISTRY.get(str(worker_type or "").lower())
        return subclass(worker_id, carbon_id) if subclass else None

    # -- the record this worker is launched from --------------------------

    def _record(self):
        return _get_worker_record(self.worker_id)

    def _resume_arguments(self, record) -> tuple[str, str]:
        """``(provider, session_id)`` to resume with, from the durable record."""
        if not record:
            return "", ""
        return (
            _normalize_provider(record.get("provider", "")),
            record.get("session_id", ""),
        )

    # -- starting ---------------------------------------------------------

    def start(self, task: str, *, resume: bool = False, incognito: bool = False) -> str:
        """Start or resume this worker. Returns the line a manager reads."""
        with file_lock(_worker_launch_lock_path(self.worker_id)):
            if self.worker_id in _load_active():
                return f"Error: Worker '{self.worker_id}' is already active."

            record = self._record()
            if self.requires_existing_record and not record:
                return f"Error: Worker '{self.worker_id}' is not registered."

            ok, result = self._launch(task, record, resume=resume, incognito=incognito)
        if ok:
            return result
        return self._on_launch_failed(result)

    def _launch(self, task, record, *, resume: bool, incognito: bool):
        provider, session_id = self._resume_arguments(record)
        if resume:
            return _launch_worker_process(
                self.worker_id,
                task,
                self.worker_type,
                self.carbon_id,
                incognito=incognito,
                resume=True,
                provider=provider or "claude",
                session_id=session_id,
            )
        return _launch_with_provider_order(
            self.worker_id,
            task,
            self.worker_type,
            self.carbon_id,
            incognito=incognito,
        )

    def _on_launch_failed(self, result: str) -> str:
        """What to do when the launch did not take. Most kinds do nothing."""
        return result
