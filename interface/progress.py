"""Progress frames shown to a contact while work is in flight.

Builds the small, schema-versioned events the interface renders — reading a
file, executing, thinking, done — and sanitizes every one of them on the way
out. What counts as unsafe, and how it is removed, lives in
:mod:`interface.redaction`.

Nothing here knows which provider produced an event. Normalizing a raw stream
into this vocabulary is each provider's own job, behind
``InferenceProvider.progress_events``.
"""
import time

from interface.redaction import (
    _ADVERTISING_CONTENT_MARKER,
    _COMMAND_MARKER,
    _COMMAND_OUTPUT_MARKER,
    _PRIVATE_MANAGER_MARKER,
    compact,
    contains_advertising_memory_reference,
    contains_private_manager_tool,
    redact_diagnostic_text,
)

PROGRESS_SCHEMA_VERSION = 1

READING_FILE = "reading_file"
WRITING_FILE = "writing_file"
EXECUTING = "executing"
SEARCHING_WEB = "searching_web"
THINKING = "thinking"
DONE = "done"

DISPLAY_KINDS = {READING_FILE, WRITING_FILE, EXECUTING, SEARCHING_WEB, THINKING, DONE}
_FAILURE_STATUS_MARKERS = ("error", "failed", "timeout", "cancel")
_PROVIDER_AUTH_FAILURE_MARKERS = (
    "authentication_failed",
    "authentication failed",
    "failed to authenticate",
    "not authenticated",
    "not logged in",
    "login required",
    "please run /login",
    "oauth session expired",
    "oauth token expired",
    "invalid api key",
    "incorrect api key",
    "401 unauthorized",
)



def now_ms():
    return int(time.time() * 1000)


def sanitize_progress_event(event):
    """Remove advertising contents and private manager payloads from telemetry."""

    if not isinstance(event, dict):
        return event
    sanitized = dict(event)
    if sanitized.get("kind") == EXECUTING:
        changed = False
        if "command" in sanitized:
            sanitized["command"] = _COMMAND_MARKER
            changed = True
        for key in (
            "output",
            "preview",
            "delta",
            "error",
            "description",
        ):
            if key in sanitized:
                sanitized[key] = _COMMAND_OUTPUT_MARKER
                changed = True
        if changed:
            sanitized["content_redacted"] = True

    values = [value for value in sanitized.values() if isinstance(value, str)]
    advertising_related = any(
        contains_advertising_memory_reference(value) for value in values
    )
    private_manager = any(contains_private_manager_tool(value) for value in values)
    if not advertising_related and not private_manager:
        return sanitized

    marker = (
        _PRIVATE_MANAGER_MARKER
        if private_manager
        else _ADVERTISING_CONTENT_MARKER
    )
    for key in (
        "output",
        "preview",
        "delta",
        "summary_delta",
        "error",
        "description",
        "command",
        "query",
    ):
        if key in sanitized:
            sanitized[key] = marker
    sanitized["content_redacted"] = True
    return sanitized


def progress_is_error(event):
    """Return true when normalized provider evidence represents a failure."""
    if not isinstance(event, dict):
        return False
    if event.get("is_error"):
        return True
    status = str(event.get("status") or "").lower()
    return any(marker in status for marker in _FAILURE_STATUS_MARKERS)



def provider_authentication_failed(*values):
    """Return true only for explicit provider authentication evidence."""
    text = " ".join(str(value or "") for value in values).lower()
    return any(marker in text for marker in _PROVIDER_AUTH_FAILURE_MARKERS)


def provider_not_authenticated_message(provider):
    """Return a stable provider-specific message safe for Carbon visibility."""
    normalized = str(provider or "").strip().lower()
    labels = {
        "claude": "Claude",
        "codex": "Codex",
        "chatgpt": "Codex",
    }
    label = labels.get(normalized, normalized.title() or "Provider")
    return f"{label} not authenticated."


def diagnostic_error_summary(event, limit=500):
    """Return a compact, credential-redacted provider failure summary."""
    if not progress_is_error(event):
        return ""
    value = (
        event.get("error")
        or event.get("preview")
        or event.get("output")
        or event.get("status")
        or "provider error"
    )
    return redact_diagnostic_text(value, limit=limit)


def progress_event(provider, kind, **fields):
    event = {
        "schema": "silicon.progress",
        "version": PROGRESS_SCHEMA_VERSION,
        "provider": provider,
        "kind": kind,
        "ts_ms": now_ms(),
    }
    for key, value in fields.items():
        if value is not None and value != "":
            event[key] = value
    return sanitize_progress_event(event)



def _first_present(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if value:
            return value
    return ""


# --- Phase 2: provider token harvest (additive; existing consumers unaffected) ---
# Memo Sections 2.2 (Gaps 1/2), 4.2 (Claude primary), 4.3 (Codex best-effort).
# These helpers attach a normalized usage dict to the emitted DONE event. No
# existing field is removed or changed; progress_display_line() ignores the new
# key, so terminal output is byte-identical to before.

def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0



def progress_display_line(event):
    if not event or event.get("kind") not in DISPLAY_KINDS:
        return ""

    kind = event.get("kind")
    status = event.get("status")

    if kind == THINKING:
        return "thinking"

    if kind == READING_FILE:
        target = event.get("path") or event.get("preview") or ""
        if status == "completed":
            return f"reading file done: {compact(target, 160)}"
        return f"reading file: {compact(target, 160)}"

    if kind == WRITING_FILE:
        target = event.get("path") or event.get("preview") or ""
        if status == "completed":
            return f"writing file done: {compact(target, 160)}"
        if status == "updated":
            return f"writing file updated: {compact(target, 160)}"
        return f"writing file: {compact(target, 160)}"

    if kind == EXECUTING:
        failed = event.get("is_error") or status == "error"
        exit_code = event.get("exit_code")
        if exit_code is not None:
            try:
                failed = failed or int(exit_code) != 0
            except (TypeError, ValueError):
                failed = True
        if failed:
            output = event.get("error") or event.get("preview") or event.get("output") or ""
            suffix = f": {compact(output, 180)}" if output else ""
            return f"executing command failed{suffix}"
        return "executing command"

    if kind == SEARCHING_WEB:
        target = event.get("query") or event.get("preview") or ""
        if status == "completed":
            return f"searching web done: {compact(target, 160)}"
        return f"searching web: {compact(target, 160)}"

    if kind == DONE:
        status = event.get("status", "")
        parts = ["done" if status in ("completed", "success", "") else f"done {status}"]
        if event.get("duration_ms") is not None:
            parts.append(f"{event.get('duration_ms') / 1000:.1f}s")
        if event.get("cost_usd") is not None:
            parts.append(f"${event.get('cost_usd'):.4f}")
        if event.get("error"):
            parts.append(f"error={event.get('error')}")
        return " ".join(parts)

    return ""


def usage_from_done_event(event):
    """Unified token/cost view for the diagnostics tracer (memo Section 4.4).

    Maps a normalized DONE event onto the keyword arguments accepted by the
    Diagnostics span.set_tokens(...) API, so a caller can record a provider
    call in one line:

        for ev in provider.progress_events(raw, state):
            if ev.get("kind") == DONE:
                span.set_tokens(**usage_from_done_event(ev))

    Safe on any event: missing pieces default to zero / None. Returns {} when
    the event is not a DONE event so callers can splat unconditionally.
    """
    if not event or event.get("kind") != DONE:
        return {}
    usage = event.get("usage") or {}
    out = {
        "input": _as_int(usage.get("input")),
        "output": _as_int(usage.get("output")),
        "cache_read": _as_int(usage.get("cache_read")),
        "cache_creation": _as_int(usage.get("cache_creation")),
        "cost_usd": float(event.get("cost_usd") or 0.0),
        "provider_duration_ms": event.get("duration_ms"),
    }
    if usage.get("num_turns") is not None:
        out["num_turns"] = usage.get("num_turns")
    if event.get("model"):
        out["model"] = event.get("model")
    if event.get("model_provider"):
        out["model_provider"] = event.get("model_provider")
    return out
