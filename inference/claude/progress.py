"""Claude's stream, in Silicon's progress vocabulary.

Only this file knows what a Claude stream-json event looks like. Everything
downstream sees the normalized events defined in :mod:`interface.progress`.
"""
from __future__ import annotations

import json

from interface.progress import (
    DONE,
    EXECUTING,
    READING_FILE,
    SEARCHING_WEB,
    THINKING,
    WRITING_FILE,
    _as_int,
    _first_present,
    progress_event,
)
from interface.redaction import (
    _ADVERTISING_CONTENT_MARKER,
    _LOG_CALLS_KEY,
    _log_output_limit,
    _log_safe,
    _output_block,
    _reads_private_content,
    _tool_call_summary,
    compact,
    redact_private_manager_output,
    stringify_command,
)


def _normalize_claude_usage(event):
    """Read the Claude 'result' event usage block into a normalized token dict.

    Claude stream-json terminal 'result' events carry a top-level 'usage' object
    (input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens) plus a top-level 'num_turns'. Returns None when
    no usage block is present, so progress_event() simply omits the key.
    """
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    normalized = {
        "input": _as_int(usage.get("input_tokens")),
        "output": _as_int(usage.get("output_tokens")),
        "cache_read": _as_int(usage.get("cache_read_input_tokens")),
        "cache_creation": _as_int(usage.get("cache_creation_input_tokens")),
    }
    if event.get("num_turns") is not None:
        normalized["num_turns"] = _as_int(event.get("num_turns"))
    return normalized


# Codex token schema, LOCKED against a real captured event (2026-06-26, memo Q1).
# Tokens do NOT ride on turn/completed; they arrive on a separate
# `thread/tokenUsage/updated` notification whose params.tokenUsage carries two
# blocks:
#     "total" -> cumulative across the whole thread
#     "last"  -> this turn only   (the one we want; "use last, not total")
# Real shape:
#     {"totalTokens","inputTokens","cachedInputTokens","outputTokens",
#      "reasoningOutputTokens"}
#
# Critical cross-provider difference: Codex reports cachedInputTokens as a
# SUBSET of inputTokens (confirmed by totalTokens == inputTokens + outputTokens,
# with cachedInputTokens NOT added on top). Claude is the opposite -- its
# cache_read_input_tokens is a separate bucket excluded from input_tokens. The
# diagnostics rollup defines total = input + output + cache_read + cache_creation
# (see core/diagnostics _Tokens.total), so to keep the four buckets meaning the
# same thing across providers we subtract the cached portion back out of input.
# Then the four-way sum reconstructs Codex's totalTokens exactly:
#     (inputTokens - cachedInputTokens) + outputTokens + cachedInputTokens + 0
#       = inputTokens + outputTokens = totalTokens

def _claude_tool_progress(block):
    tool_name = block.get("name", "")
    tool_input = block.get("input") or {}
    item_id = block.get("id")

    if tool_name in {"Read", "Glob", "Grep", "LS", "NotebookRead"}:
        path = _first_present(tool_input, ["file_path", "path", "notebook_path"]) or tool_input.get("pattern")
        return progress_event("claude", READING_FILE, status="started", item_id=item_id, tool_name=tool_name, path=path)

    if tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        path = _first_present(tool_input, ["file_path", "path", "notebook_path"])
        return progress_event("claude", WRITING_FILE, status="started", item_id=item_id, tool_name=tool_name, path=path)

    if tool_name in {"WebSearch", "WebFetch"}:
        query = _first_present(tool_input, ["query", "url"])
        return progress_event("claude", SEARCHING_WEB, status="started", item_id=item_id, tool_name=tool_name, query=query)

    if tool_name == "Bash":
        command = stringify_command(tool_input.get("command"))
        return progress_event(
            "claude",
            EXECUTING,
            status="started",
            item_id=item_id,
            tool_name=tool_name,
            command=command,
            description=tool_input.get("description"),
        )

    return progress_event("claude", EXECUTING, status="started", item_id=item_id, tool_name=tool_name)


def claude_progress_events(event, state=None):
    state = state if state is not None else {}
    etype = event.get("type", "")
    events = []

    if etype == "system" and event.get("subtype") == "init":
        if event.get("model"):
            state["claude_model"] = str(event["model"])

    elif etype == "assistant":
        for block in event.get("message", {}).get("content", []):
            btype = block.get("type", "")
            if btype == "thinking":
                events.append(progress_event("claude", THINKING, status="started"))
            elif btype == "tool_use":
                progress = _claude_tool_progress(block)
                item_id = progress.get("item_id")
                if item_id:
                    state.setdefault("items", {})[item_id] = progress
                events.append(progress)

    elif etype == "user":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            item_id = block.get("tool_use_id")
            started = state.get("items", {}).get(item_id, {})
            kind = started.get("kind", EXECUTING)
            content = block.get("content", "")
            events.append(progress_event(
                "claude",
                kind,
                status="completed",
                item_id=item_id,
                tool_name=started.get("tool_name"),
                path=started.get("path"),
                query=started.get("query"),
                command=started.get("command"),
                is_error=block.get("is_error", False),
                output=content,
                preview=compact(content),
            ))

    elif etype == "result":
        result = event.get("result", "")
        events.append(progress_event(
            "claude",
            DONE,
            status=event.get("subtype") or ("error" if event.get("is_error") else "success"),
            is_error=event.get("is_error", False),
            duration_ms=event.get("duration_ms"),
            cost_usd=event.get("total_cost_usd") or event.get("cost_usd"),
            usage=_normalize_claude_usage(event),
            preview=redact_private_manager_output(result),
            model=state.get("claude_model"),
            model_provider="anthropic",
        ))

    return events



def claude_log_lines(event, state=None):
    """The operator's view of one raw Claude stream event.

    :func:`progress_event` sanitizes commands and output at construction — they
    are replaced with markers before they can reach a Carbon or telemetry, which
    is why the progress surface only ever says "executing command". The raw
    stream event never leaves this process, so it is the only place a log with
    real commands and real output can come from.

    Returns a list of lines; output is indented so it stays readable.
    """
    state = state if state is not None else {}
    calls = state.setdefault(_LOG_CALLS_KEY, {})
    limit = _log_output_limit()
    etype = event.get("type", "")
    lines = []

    if etype == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "?")
            raw_summary, description = _tool_call_summary(name, block.get("input"))
            private = _reads_private_content(
                raw_summary, json.dumps(block.get("input"), default=str)
            )
            summary = _log_safe(raw_summary)
            calls[block.get("id")] = {
                "name": name,
                "summary": summary,
                "private": private,
            }
            lines.append(f"{name}: {summary}" if summary else f"{name}")
            if description:
                lines.append(f"    ({_log_safe(description)})")
        return lines

    if etype == "user":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            call = calls.pop(block.get("tool_use_id"), {})
            name = call.get("name") or "tool"
            summary = call.get("summary") or ""
            failed = bool(block.get("is_error"))
            head = f"{name} {'FAILED' if failed else 'done'}"
            if summary:
                head += f": {summary}"
            lines.append(head)
            if call.get("private"):
                lines.append(f"    │ {_ADVERTISING_CONTENT_MARKER}")
                continue
            lines.extend(_output_block(block.get("content"), limit))
        return lines

    return lines


