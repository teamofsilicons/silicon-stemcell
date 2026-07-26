import json
import posixpath
import re
import time


PROGRESS_SCHEMA_VERSION = 1

READING_FILE = "reading_file"
WRITING_FILE = "writing_file"
EXECUTING = "executing"
SEARCHING_WEB = "searching_web"
THINKING = "thinking"
DONE = "done"

DISPLAY_KINDS = {READING_FILE, WRITING_FILE, EXECUTING, SEARCHING_WEB, THINKING, DONE}
_FAILURE_STATUS_MARKERS = ("error", "failed", "timeout", "cancel")
_PRIVATE_MANAGER_MARKER = "[private manager tool invocation omitted]"
_ADVERTISING_CONTENT_MARKER = "[advertising memory content omitted]"
_COMMAND_MARKER = "[command omitted]"
_COMMAND_OUTPUT_MARKER = "[command output omitted]"
_ADVERTISING_PATH_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9._-])prompts[/\\]+advertising(?:[/\\]|$)"
)
_DRAFT_ARCHIVE_PATH_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9._-])core[/\\]+interface_state[/\\]+"
    r"team_context_drafts(?:[/\\]|$)"
)
_PATH_TOKEN_RE = re.compile(
    r"(?:[A-Za-z]:)?[/\\]?[A-Za-z0-9._*?\[\]-]+"
    r"(?:[/\\]+[A-Za-z0-9._*?\[\]-]+)+"
)
_SENSITIVE_ERROR_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(?:sk|sct|scs|ghp|github_pat)_[a-z0-9_-]{8,}\b"), "[redacted credential]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted credential]"),
    (
        re.compile(
            r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|password)"
            r"\s*[:=]\s*['\"]?[^\s,'\"]+"
        ),
        r"\1=[redacted]",
    ),
)


def now_ms():
    return int(time.time() * 1000)


def compact(text, limit=240):
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        return text[:limit - 1] + "..."
    return text


def contains_private_manager_tool(value):
    text = str(value or "")
    # JSON's solidus escape is semantically identical to "/". Normalize it
    # before the malformed/plain-text fallback so a truncated invocation still
    # fails closed instead of printing its content.
    normalized_text = re.sub(r"\\u002[fF]", "/", text).replace("\\/", "/")
    decoder = json.JSONDecoder()
    index = 0
    while index < len(normalized_text):
        start = normalized_text.find("{", index)
        if start < 0:
            break
        try:
            parsed, consumed = decoder.raw_decode(normalized_text[start:])
        except (json.JSONDecodeError, ValueError):
            index = start + 1
            continue
        index = start + max(consumed, 1)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("tools"), list):
            continue
        for tool_spec in parsed["tools"]:
            if not isinstance(tool_spec, dict):
                continue
            tool = str(tool_spec.get("tool") or "")
            if (
                tool == "advertising_memory/update"
                or tool == "work_update"
                or tool in {"trust/list", "trust/get", "trust/set"}
                or tool == "extend"
                or tool.startswith("extend/")
            ):
                return True
    # Fail closed for incomplete/plain private invocations that still use the
    # canonical spelling. Complete escaped JSON is handled by raw_decode above.
    return (
        "advertising_memory/update" in normalized_text
        or bool(
            re.search(
                r"""["']tool["']\s*:\s*["']work_update["']""",
                normalized_text,
            )
        )
        or bool(
            re.search(
                r"""["']tool["']\s*:\s*["']trust/(?:list|get|set)["']""",
                normalized_text,
            )
        )
        or bool(
            re.search(
                r"""["']tool["']\s*:\s*["']extend(?:/|["'])""",
                normalized_text,
            )
        )
    )


def contains_advertising_memory_reference(value):
    text = re.sub(r"\\u002[fF]", "/", str(value or "")).replace("\\/", "/")
    # Inspect a quote-stripped copy as well: shell commands can legally quote
    # individual path components. Lexically collapse "." and ".." segments so
    # aliases such as prompts/x/../advertising cannot evade content redaction.
    candidates = [text, text.replace('"', "").replace("'", "")]
    normalized_candidates: list[str] = []
    for candidate in candidates:
        slash_normalized = candidate.replace("\\", "/")
        normalized_candidates.append(slash_normalized)
        normalized_candidates.extend(
            posixpath.normpath(match.group(0))
            for match in _PATH_TOKEN_RE.finditer(slash_normalized)
        )
    text = "\n".join(normalized_candidates)
    return bool(
        _ADVERTISING_PATH_RE.search(text)
        or _DRAFT_ARCHIVE_PATH_RE.search(text)
    )


def redact_private_manager_output(value, limit=240):
    """Keep private manager-tool payloads out of progress and terminal logs."""
    text = str(value or "")
    if contains_private_manager_tool(text):
        return _PRIVATE_MANAGER_MARKER
    if contains_advertising_memory_reference(text):
        return _ADVERTISING_CONTENT_MARKER
    return compact(text, limit=limit)


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


def redact_diagnostic_text(value, limit=500):
    """Bound diagnostic text and redact common credential formats."""
    text = " ".join(str(value or "").split())
    private_safe = redact_private_manager_output(text, limit=limit)
    if private_safe in {_PRIVATE_MANAGER_MARKER, _ADVERTISING_CONTENT_MARKER}:
        return private_safe
    for pattern, replacement in _SENSITIVE_ERROR_PATTERNS:
        text = pattern.sub(replacement, text)
    return compact(text, limit=limit)


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


def write_progress_line(path, event):
    if not path or not event:
        return
    event = sanitize_progress_event(event)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def stringify_command(command):
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


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
    Diagnostics span.set_tokens(...) API, so manager.py can record a provider
    call in one line:

        for ev in claude_progress_events(raw, state):
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
