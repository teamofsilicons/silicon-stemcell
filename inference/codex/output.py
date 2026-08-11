"""Reading a detached Codex worker's output file back.

Codex reports the same run in two dialects — the older ``type:`` events and the
app-server's ``method:`` notifications — so both are accepted here.
"""
from __future__ import annotations

from inference.claude.output import authentication_message
from inference.models import WorkerOutcome
from inference.parsing import json_events


def _is_turn_completed(event: dict) -> bool:
    return (
        event.get("type") == "turn.completed"
        or event.get("method") == "turn/completed"
    )


def session_id(events) -> str:
    """The thread id, wherever in the stream Codex chose to reveal it."""
    for event in events:
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return event["thread_id"]
        if event.get("method") == "thread/started":
            thread_id = event.get("params", {}).get("thread", {}).get("id")
            if thread_id:
                return thread_id
        result = event.get("result")
        if isinstance(result, dict):
            thread_id = (result.get("thread") or {}).get("id")
            if thread_id:
                return thread_id
    return ""


def _terminal_state(events) -> tuple[bool, str]:
    if any(event.get("type") == "silicon.codex_app_error" for event in events):
        return any(_is_turn_completed(event) for event in events), "failed"
    completed = [event for event in events if _is_turn_completed(event)]
    if not completed:
        return False, "failed"
    terminal = completed[-1]
    turn = (terminal.get("params") or {}).get("turn") or {}
    status = str(turn.get("status") or terminal.get("status") or "").lower()
    return True, "completed" if status in {"", "completed", "success"} else "failed"


def _summary(events) -> str:
    texts = []
    streamed_text = ""
    tool_lines = []
    reasoning_lines = []
    error_lines = []
    token_summary = ""

    for event in events:
        if event.get("type") == "silicon.codex_app_error":
            message = event.get("message", "").strip()
            if message:
                error_lines.append(message)

        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "").strip()
                if text:
                    texts.append(text)
            continue

        method = event.get("method", "")
        params = event.get("params", {})

        if method == "item/agentMessage/delta":
            streamed_text += params.get("delta", "")

        elif method == "item/completed":
            item = params.get("item", {})
            item_type = item.get("type", "")
            if item_type == "agentMessage":
                text = item.get("text", "").strip()
                if text:
                    texts.append(text)
            elif item_type == "commandExecution":
                command = item.get("command") or item.get("cmd") or ""
                status = item.get("status") or item.get("exitCode") or "completed"
                if isinstance(command, list):
                    command = " ".join(str(part) for part in command)
                if command:
                    tool_lines.append(f"Command {status}: {command}")
            elif item_type == "mcpToolCall":
                name = item.get("name") or item.get("toolName") or "mcp tool"
                status = item.get("status") or "completed"
                tool_lines.append(f"Tool {status}: {name}")
            elif item_type == "fileChange":
                path = item.get("path") or item.get("filePath") or "file change"
                status = item.get("status") or "completed"
                tool_lines.append(f"File {status}: {path}")

        elif method == "item/reasoning/summaryTextDelta":
            delta = params.get("delta", "").strip()
            if delta:
                reasoning_lines.append(delta)

        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {}).get("total", {})
            total = usage.get("totalTokens")
            if total is not None:
                token_summary = (
                    f"Token usage: total={total}, "
                    f"input={usage.get('inputTokens')}, "
                    f"output={usage.get('outputTokens')}"
                )

        elif method == "error":
            message = params.get("message") or params.get("error", {}).get(
                "message", ""
            )
            if message:
                error_lines.append(message.strip())

    if texts:
        result = texts[-1]
    elif streamed_text.strip():
        result = streamed_text.strip()
    elif error_lines:
        result = "Codex worker error: " + error_lines[-1]
    elif any(_is_turn_completed(event) for event in events):
        result = "Worker completed with no text output."
    else:
        result = "Worker running, no text output yet."

    details = []
    if tool_lines:
        details.append("Activity:\n" + "\n".join(tool_lines[-8:]))
    if reasoning_lines and not texts:
        details.append("Reasoning:\n" + " ".join(reasoning_lines[-6:]))
    if token_summary:
        details.append(token_summary)
    if error_lines and not result.startswith("Codex worker error:"):
        details.append("Errors:\n" + "\n".join(error_lines[-3:]))

    if details:
        return result + "\n\n" + "\n\n".join(details)
    return result


def read(raw: str) -> WorkerOutcome:
    """What a Codex worker's output says about how it went."""
    if not str(raw or "").strip():
        return WorkerOutcome(result="No output yet.")

    events = json_events(raw)
    if not events:
        return WorkerOutcome(result="No parseable output yet.")

    completed, state = _terminal_state(events)
    outcome = WorkerOutcome(
        completed=completed,
        state=state,
        session_id=session_id(events),
    )

    auth_message = authentication_message(events, "codex")
    if auth_message:
        outcome.auth_message = auth_message
        outcome.result = auth_message
        return outcome

    outcome.result = _summary(events)
    return outcome
