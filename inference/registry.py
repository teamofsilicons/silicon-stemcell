"""Provider name to provider class.

Adding a provider is adding a folder under ``inference/`` and one line here.
Nothing outside this module needs to learn the name.
"""
from __future__ import annotations

from inference.base import InferenceProvider


def _providers() -> dict[str, type[InferenceProvider]]:
    from inference.claude import ClaudeProvider
    from inference.codex import CodexProvider

    return {
        ClaudeProvider.name: ClaudeProvider,
        CodexProvider.name: CodexProvider,
    }


_INSTANCES: dict[str, InferenceProvider] = {}


def provider_names() -> set[str]:
    """Every provider that can be named in ``silicon.json``."""
    return set(_providers())


def get_provider(name: str) -> InferenceProvider:
    """The shared instance for a provider name.

    Providers hold no per-turn state, so one instance serves every caller.
    """
    key = str(name or "").strip().lower()
    classes = _providers()
    if key not in classes:
        raise ValueError(f"Unknown inference provider: {name!r}")
    if key not in _INSTANCES:
        _INSTANCES[key] = classes[key]()
    return _INSTANCES[key]
