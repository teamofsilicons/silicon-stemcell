import json
import time


PROGRESS_SCHEMA_VERSION = 1

READING_FILE = "reading_file"
WRITING_FILE = "writing_file"
EXECUTING = "executing"
SEARCHING_WEB = "searching_web"
THINKING = "thinking"
DONE = "done"

DISPLAY_KINDS = {READING_FILE, WRITING_FILE, EXECUTING, SEARCHING_WEB, THINKING, DONE}


def now_ms():
    return int(time.time() * 1000)


def compact(text, limit=240):
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        return text[:limit - 1] + "..."
    return text


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
    return event


def write_progress_line(path, event):
    if not path or not event:
        return
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

    if etype == "assistant":
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
            preview=compact(result),
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
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method == "turn/completed":
        turn = params.get("turn") or {}
        return progress_event(
            "codex",
            DONE,
            status=turn.get("status"),
            duration_ms=turn.get("durationMs"),
            error=(turn.get("error") or {}).get("message") if isinstance(turn.get("error"), dict) else None,
        )

    if method == "item/started":
        item = params.get("item") or {}
        item_id = item.get("id") or params.get("itemId")
        item_type = item.get("type", "item")
        kind = _codex_kind_for_item(item)
        label = codex_item_label(item)
        if item_id and kind:
            state.setdefault("items", {})[item_id] = {"kind": kind, "label": label, "item_type": item_type}
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
        preview = event.get("preview")
        return f"thinking: {preview}" if preview else "thinking"

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
        if status == "output":
            return f"executing output: {event.get('preview', '')}"
        command = event.get("command") or event.get("preview") or ""
        if status == "completed":
            bits = [f"executing done: {compact(command, 120)}"]
            if event.get("exit_code") is not None:
                bits.append(f"exit={event.get('exit_code')}")
            if event.get("preview"):
                bits.append(f"output={event.get('preview')}")
            return " ".join(bits)
        return f"executing: {compact(command, 160)}"

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
