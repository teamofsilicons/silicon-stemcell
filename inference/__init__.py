"""How Silicon thinks.

One provider per folder, one contract in :mod:`inference.base`, one facade in
:class:`Inference`. Nothing outside this package names a provider except
``silicon.json``.
"""
from inference.base import InferenceProvider
from inference.config import brain, brain_order
from inference.errors import (
    TIMEOUT_MSG,
    ProviderTimeoutError,
    error_tools,
    is_rate_limit,
    not_authenticated_tools,
)
from inference.facade import Inference, provider_failed
from inference.models import (
    STDIN_NONE,
    STDIN_STREAM,
    STDIN_TASK,
    TurnRequest,
    TurnResult,
    WorkerCommand,
    WorkerLaunchSpec,
    WorkerOutcome,
)
from inference.parsing import json_events, parse_manager_output, stream_json_user
from inference.registry import get_provider, provider_names
from inference.sessions import SESSIONS_DIR

__all__ = [
    "Inference",
    "InferenceProvider",
    "ProviderTimeoutError",
    "SESSIONS_DIR",
    "STDIN_NONE",
    "STDIN_STREAM",
    "STDIN_TASK",
    "TIMEOUT_MSG",
    "TurnRequest",
    "TurnResult",
    "WorkerCommand",
    "WorkerLaunchSpec",
    "WorkerOutcome",
    "brain",
    "brain_order",
    "error_tools",
    "get_provider",
    "is_rate_limit",
    "json_events",
    "not_authenticated_tools",
    "parse_manager_output",
    "provider_failed",
    "provider_names",
    "stream_json_user",
]
