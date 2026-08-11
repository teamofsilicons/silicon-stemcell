"""The writer worker.

Nothing of its own: writing is the base lifecycle, and the difference from a
terminal worker is entirely in its prompt and its provider command.
"""
from __future__ import annotations

from worker.base import Worker


class WriterWorker(Worker):
    worker_type = "writer"
