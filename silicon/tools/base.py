"""What a tool is, and how one is found.

A manager answers with a list of tool invocations. Each is a class here: it
declares the name it answers to, and runs. A tool that matches on a prefix
(``cron/``, ``worker``) declares that instead, and is only consulted after every
exact name has failed — a specific tool always wins.

A subclass registers itself by declaring ``name`` or ``prefix``; nothing in this
module imports one, which is what lets a new tool be a new file.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, ClassVar

_BY_NAME: dict[str, "Tool"] = {}
_BY_PREFIX: list[tuple[str, "Tool"]] = []


class Tool(ABC):
    """One thing a manager can ask Silicon to do."""

    #: The exact tool name a manager writes.
    name: ClassVar[str] = ""

    #: Matched only after every exact name has failed.
    prefix: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if cls.name:
            _BY_NAME[cls.name] = cls()
        if cls.prefix:
            _BY_PREFIX.append((cls.prefix, cls()))

    @abstractmethod
    def run(self, spec: dict, contact_id: str):
        """Do it. Returns the result a manager reads, or None for do_nothing."""


def register(name: str = "", prefix: str = "") -> Callable:
    """Register a plain handler function as a tool.

    The handlers moved out of main.py keep their bodies verbatim; this wraps one
    without asking it to become a class first.
    """

    def decorate(handler):
        attributes = {"name": name, "prefix": prefix,
                      "run": staticmethod(lambda spec, contact_id: handler(spec, contact_id))}
        type(f"{handler.__name__.lstrip('_').title().replace('_', '')}Tool",
             (Tool,), attributes)
        return handler

    return decorate


def resolve(tool_name: str) -> "Tool | None":
    """The tool that answers to this name, exact match first."""
    tool = _BY_NAME.get(tool_name)
    if tool is not None:
        return tool
    for prefix, candidate in _BY_PREFIX:
        if tool_name.startswith(prefix):
            return candidate
    return None


def names() -> list[str]:
    """Every exact tool name registered, for the doc test and diagnostics."""
    return sorted(_BY_NAME)
