"""What ``silicon.json`` says about which brain to think with.

``brain`` is the normal provider. ``brain_order`` lists it plus true fallbacks,
tried in order and only after the one above fails before producing a usable
answer. Both are operator-edited on disk, so they are validated rather than
trusted.
"""
from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel, Field, field_validator

from helpers.paths import DATA_ROOT

SILICON_CONFIG_FILE = os.path.join(os.fspath(DATA_ROOT), "silicon.json")

DEFAULT_PROVIDER = "claude"
# `chatgpt` is the name Carbons use for the same provider.
PROVIDER_ALIASES = {"chatgpt": "codex"}


def _known_providers() -> set[str]:
    from inference.registry import provider_names

    return provider_names()


class BrainConfig(BaseModel):
    """The provider-selection slice of ``silicon.json``."""

    brain: str = DEFAULT_PROVIDER
    brain_order: list[str] = Field(default_factory=list)

    @field_validator("brain", mode="before")
    @classmethod
    def _one_provider(cls, value: Any) -> str:
        return _normalize(value) or DEFAULT_PROVIDER

    @field_validator("brain_order", mode="before")
    @classmethod
    def _provider_list(cls, value: Any) -> list[str]:
        raw = [value] if isinstance(value, str) else value
        if not isinstance(raw, list):
            return []
        ordered: list[str] = []
        for item in raw:
            name = _normalize(item)
            if name and name not in ordered:
                ordered.append(name)
        return ordered

    def order(self) -> list[str]:
        """The providers to try, best first, never empty."""
        return self.brain_order or [self.brain]


def _normalize(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    name = PROVIDER_ALIASES.get(value.strip().lower(), value.strip().lower())
    return name if name in _known_providers() else ""


def read_silicon_config() -> dict[str, Any]:
    """The raw ``silicon.json`` object, or ``{}`` when it is absent or broken."""
    if not os.path.exists(SILICON_CONFIG_FILE):
        return {}
    try:
        with open(SILICON_CONFIG_FILE) as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def brain_config() -> BrainConfig:
    return BrainConfig.model_validate(read_silicon_config())


def brain() -> str:
    """The configured provider, defaulting to Claude for compatibility."""
    return brain_config().brain


def brain_order() -> list[str]:
    """The configured provider order: the brain first, then its fallbacks."""
    return brain_config().order()
