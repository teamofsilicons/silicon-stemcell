"""Reading structure back out of provider text.

Providers answer in prose that happens to contain JSON. Both the manager's
tool blocks and a worker's JSONL event stream are recovered here, so a new
provider inherits the parsing instead of restating it.
"""
from __future__ import annotations

import json


def parse_manager_output(output, debug: bool = False) -> dict | None:
    """Extract every ``{"tools": [...]}`` block from a manager's text output.

    The manager may emit several blocks in one answer. They are merged into a
    single ``{"tools": [...]}``, or ``None`` when the text holds no tools.
    """
    if debug:
        print(f"[DEBUG] Raw manager output:\n{output}\n", flush=True)

    if not output:
        return None

    all_tools: list[dict] = []
    text = str(output)
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            parsed, consumed = decoder.raw_decode(text[start:])
        except (json.JSONDecodeError, ValueError):
            index = start + 1
            continue
        index = start + max(consumed, 1)
        if not isinstance(parsed, dict):
            continue
        tools = parsed.get("tools")
        if not isinstance(tools, list):
            continue
        all_tools.extend(
            tool_spec for tool_spec in tools if isinstance(tool_spec, dict)
        )

    if all_tools:
        return {"tools": all_tools}
    return None


def json_events(raw) -> list[dict]:
    """Every JSON object on its own line, skipping anything unparseable."""
    events: list[dict] = []
    text = str(raw or "")
    if not text.strip():
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def stream_json_user(text) -> str:
    """One user message in the format ``--input-format=stream-json`` expects."""
    return json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": str(text or "")}],
            },
        }
    ) + "\n"
