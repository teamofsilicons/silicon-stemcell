"""What a running turn tells the rest of Silicon about itself.

Progress lines go to whoever is watching, token usage goes to the diagnosis
span, and files the provider wrote go to the journal. All three are best
effort: a turn must never fail because nobody was listening.
"""
from __future__ import annotations

import os
from contextlib import nullcontext

from core.progress import (
    DONE,
    WRITING_FILE,
    diagnostic_error_summary,
    progress_display_line,
    progress_is_error,
    usage_from_done_event,
)


def provider_span(trace, provider: str):
    """A provider span, or a no-op when diagnostics are unavailable."""
    if trace is None:
        return nullcontext()
    try:
        span = trace.span("provider_call")
        span.set_meta(provider=provider)
        return span
    except Exception:
        return nullcontext()


def attach_usage(span, progress) -> None:
    """Record a completed turn's token usage and error status on its span."""
    if span is None or not progress or progress.get("kind") != DONE:
        return
    try:
        span.set_tokens(**usage_from_done_event(progress))
        span.set_meta(
            provider_status=str(progress.get("status") or ""),
            provider_is_error=bool(progress_is_error(progress)),
        )
        if progress_is_error(progress):
            span.status = "error"
            summary = diagnostic_error_summary(progress)
            if summary:
                span.set_meta(error=summary)
    except Exception:
        pass


def notify_progress(on_progress, progress) -> None:
    """Forward normalized provider progress without letting prose drive the UI."""
    if not on_progress or not progress:
        return
    line = progress_display_line(progress)
    if not line:
        return
    try:
        on_progress(progress, line)
    except TypeError:
        try:
            on_progress(line)
        except Exception:
            pass
    except Exception:
        pass


def record_file_write(progress, env, tag: str) -> None:
    """Journal a file this run wrote, for the diagnosis store.

    The provider stream is the only place a Write/Edit is visible — the file is
    changed by the provider's own tools, not by Silicon. ``WRITING_FILE``
    progress events carry the path, so this is where "every file it writes"
    becomes knowable.
    """
    if not progress or progress.get("kind") != WRITING_FILE:
        return
    if progress.get("status") != "started":
        return
    path = progress.get("path")
    if not path:
        return
    try:
        from core.iwantto import journal
        from core.iwantto.actor import CONTACT_ENV, ID_ENV, KIND_ENV

        source = env if env is not None else os.environ
        journal.record_file_write(
            path,
            kind=str(source.get(KIND_ENV) or ""),
            actor_id=str(source.get(ID_ENV) or tag),
            contact_id=str(source.get(CONTACT_ENV) or ""),
            tool=str(progress.get("tool_name") or ""),
        )
    except Exception:
        pass
