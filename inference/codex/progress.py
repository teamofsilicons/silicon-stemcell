"""The Codex app-server's notifications, in Silicon's progress vocabulary.

Only this file knows what an app-server notification looks like. Everything
downstream sees the normalized events defined in :mod:`interface.progress`.
"""
from __future__ import annotations

import json

from interface.progress import (
    DISPLAY_KINDS,
    DONE,
    EXECUTING,
    READING_FILE,
    SEARCHING_WEB,
    THINKING,
    WRITING_FILE,
    _as_int,
    progress_event,
)
from interface.redaction import (
    _ADVERTISING_CONTENT_MARKER,
    _LOG_CALLS_KEY,
    _log_output_limit,
    _log_safe,
    _output_block,
    _reads_private_content,
    compact,
    contains_advertising_memory_reference,
    stringify_command,
)


_CODEX_LAST_USAGE_STATE_KEY = "codex_last_token_usage"
_CODEX_MODEL_STATE_KEY = "codex_model"
_CODEX_MODEL_PROVIDER_STATE_KEY = "codex_model_provider"


def _normalize_codex_usage(last):
    """Normalize a Codex tokenUsage `last` block into the shared token dict.

    `last` is params.tokenUsage.last from a thread/tokenUsage/updated event
    (stashed in state until turn/completed fires). Returns None when absent or
    empty, so progress_event() simply omits the usage key.
    """
    if not isinstance(last, dict):
        return None
    input_tokens = _as_int(last.get("inputTokens"))
    cached = _as_int(last.get("cachedInputTokens"))
    output_tokens = _as_int(last.get("outputTokens"))
    normalized = {
        # cachedInputTokens is a subset of inputTokens -> subtract it back out
        "input": max(0, input_tokens - cached),
        "output": output_tokens,
        "cache_read": cached,
        "cache_creation": 0,  # Codex has no cache-creation concept
    }
    if not any(normalized.values()):
        return None
    return normalized



def codex_item_label(item):
    item_type = item.get("type", "item")
    if item_type == "commandExecution":
        return stringify_command(item.get("command") or item.get("cmd") or item.get("argv"))
    if item_type == "fileChange":
        changes = item.get("changes") or []
        paths = [str(change.get("path")) for change in changes if change.get("path")]
        return ", ".join(paths) if paths else item.get("path") or item.get("filePath") or "file change"
    if item_type == "mcpToolCall":
        return f"{item.get('server') or item.get('serverName') or '?'}.{item.get('tool') or item.get('name') or '?'}"
    if item_type == "dynamicToolCall":
        return str(item.get("tool") or "dynamic tool")
    if item_type == "webSearch":
        return str(item.get("query") or item.get("action") or "web search")
    return item_type


def _codex_kind_for_item(item):
    item_type = item.get("type", "item")
    label = codex_item_label(item)
    label_lower = label.lower()

    if item_type == "commandExecution":
        return EXECUTING
    if item_type == "fileChange":
        return WRITING_FILE
    if item_type == "webSearch":
        return SEARCHING_WEB
    if item_type in {"reasoning", "plan"}:
        return THINKING
    if item_type in {"mcpToolCall", "dynamicToolCall", "collabToolCall"}:
        if any(word in label_lower for word in ("read", "grep", "glob", "list", "search_file")):
            return READING_FILE
        if any(word in label_lower for word in ("write", "edit", "patch", "update", "apply")):
            return WRITING_FILE
        if "web" in label_lower or "search" in label_lower:
            return SEARCHING_WEB
        return EXECUTING
    return ""


def codex_progress_event(msg, state=None):
    state = state if state is not None else {}
    if msg.get("type") == "silicon.codex_context":
        if msg.get("model"):
            state[_CODEX_MODEL_STATE_KEY] = str(msg["model"])
        if msg.get("model_provider"):
            state[_CODEX_MODEL_PROVIDER_STATE_KEY] = str(msg["model_provider"])
        return None
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method == "thread/tokenUsage/updated":
        # Stash this turn's `last` usage block so it's available when
        # turn/completed fires. Not itself a display event.
        usage_block = params.get("tokenUsage") or {}
        last = usage_block.get("last")
        if isinstance(last, dict) and last:
            state[_CODEX_LAST_USAGE_STATE_KEY] = last
        return None

    if method == "turn/completed":
        turn = params.get("turn") or {}
        return progress_event(
            "codex",
            DONE,
            status=turn.get("status"),
            duration_ms=turn.get("durationMs"),
            usage=_normalize_codex_usage(state.get(_CODEX_LAST_USAGE_STATE_KEY)),
            model=state.get(_CODEX_MODEL_STATE_KEY),
            model_provider=state.get(_CODEX_MODEL_PROVIDER_STATE_KEY),
            error=(turn.get("error") or {}).get("message") if isinstance(turn.get("error"), dict) else None,
        )

    if method == "item/started":
        item = params.get("item") or {}
        item_id = item.get("id") or params.get("itemId")
        item_type = item.get("type", "item")
        kind = _codex_kind_for_item(item)
        label = codex_item_label(item)
        if item_id and kind:
            state.setdefault("items", {})[item_id] = {
                "kind": kind,
                "label": label,
                "item_type": item_type,
                "advertising_memory": (
                    contains_advertising_memory_reference(label)
                    or contains_advertising_memory_reference(
                        json.dumps(item, ensure_ascii=False)
                    )
                ),
            }
        if kind == READING_FILE:
            return progress_event("codex", kind, status="started", item_id=item_id, path=label)
        if kind == WRITING_FILE:
            return progress_event("codex", kind, status="started", item_id=item_id, path=label)
        if kind == SEARCHING_WEB:
            return progress_event("codex", kind, status="started", item_id=item_id, query=label)
        if kind == EXECUTING:
            return progress_event("codex", kind, status="started", item_id=item_id, command=label)
        if kind == THINKING:
            return progress_event("codex", kind, status="started", item_id=item_id)
        return None

    if method == "item/completed":
        item = params.get("item") or {}
        item_id = item.get("id") or params.get("itemId")
        remembered = state.get("items", {}).get(item_id, {})
        kind = remembered.get("kind") or _codex_kind_for_item(item)
        label = codex_item_label(item) if item else remembered.get("label", "")
        if kind not in DISPLAY_KINDS or kind == DONE:
            return None
        output = item.get("aggregatedOutput", "")
        advertising_memory = bool(
            remembered.get("advertising_memory")
            or contains_advertising_memory_reference(
                json.dumps(item, ensure_ascii=False)
            )
        )
        if advertising_memory:
            output = _ADVERTISING_CONTENT_MARKER
        return progress_event(
            "codex",
            kind,
            status="completed",
            item_id=item_id,
            path=label if kind in {READING_FILE, WRITING_FILE} else None,
            query=label if kind == SEARCHING_WEB else None,
            command=label if kind == EXECUTING else None,
            exit_code=item.get("exitCode"),
            output=output,
            preview=compact(output),
        )

    if method in {"item/commandExecution/outputDelta", "item/fileChange/outputDelta"}:
        item_id = params.get("itemId")
        remembered = state.get("items", {}).get(item_id, {})
        kind = remembered.get("kind", EXECUTING)
        if kind not in DISPLAY_KINDS or kind == DONE:
            kind = EXECUTING
        delta = params.get("delta", "")
        if remembered.get("advertising_memory"):
            delta = _ADVERTISING_CONTENT_MARKER
        return progress_event("codex", kind, status="output", item_id=item_id, delta=delta, preview=compact(delta))

    if method == "item/reasoning/summaryTextDelta":
        delta = params.get("delta", "")
        return progress_event("codex", THINKING, status="output", item_id=params.get("itemId"), summary_delta=delta, preview=compact(delta))

    if method == "item/fileChange/patchUpdated":
        return progress_event("codex", WRITING_FILE, status="updated", item_id=params.get("itemId"), path=params.get("path") or params.get("filePath"))

    if method == "error":
        err = params.get("error") or params
        return progress_event("codex", DONE, status="error", error=err.get("message") if isinstance(err, dict) else str(err))

    return None


# How much command output to show in the process log. Set
# SILICON_LOG_OUTPUT_CHARS=0 for no limit, or a small number to quieten it.

def codex_log_lines(msg, state=None):
    """The operator's view of one raw Codex app-server message."""
    state = state if state is not None else {}
    calls = state.setdefault(_LOG_CALLS_KEY, {})
    limit = _log_output_limit()
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method == "item/started":
        item = params.get("item") or {}
        item_id = item.get("id") or params.get("itemId")
        raw_label = codex_item_label(item)
        private = _reads_private_content(
            raw_label, json.dumps(item, ensure_ascii=False, default=str)
        )
        label = _log_safe(raw_label)
        name = str(item.get("type") or "item")
        calls[item_id] = {"name": name, "summary": label, "private": private}
        return [f"{name}: {label}" if label else name]

    if method == "item/completed":
        item = params.get("item") or {}
        item_id = item.get("id") or params.get("itemId")
        call = calls.pop(item_id, {})
        name = call.get("name") or str(item.get("type") or "item")
        summary = call.get("summary") or _log_safe(codex_item_label(item))
        exit_code = item.get("exitCode")
        failed = exit_code not in (None, 0)
        head = f"{name} {'FAILED' if failed else 'done'}"
        if exit_code is not None:
            head += f" exit={exit_code}"
        if summary:
            head += f": {summary}"
        private = call.get("private") or _reads_private_content(
            json.dumps(item, ensure_ascii=False, default=str)
        )
        if private:
            return [head, f"    │ {_ADVERTISING_CONTENT_MARKER}"]
        return [head, *_output_block(item.get("aggregatedOutput"), limit)]

    return []


