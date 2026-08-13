"""Importing every tool, so the registry is populated before it is consulted.

One import per file. A new tool is a new module and a new line here.
"""
from silicon.tools import brain_error, browser, cron, memory  # noqa: F401
from silicon.tools import session, trust, work, worker  # noqa: F401
