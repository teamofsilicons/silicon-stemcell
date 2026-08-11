"""Keeping private material out of durable Codex traces.

The app-server stream carries the manager's own prose, its advertising memory,
and the exact commands it ran. All three are written to a durable log that
leaves this process, so anything private is replaced by a marker before it is
written — never after.
"""
from __future__ import annotations

import json

from interface.progress import (
    contains_advertising_memory_reference,
    contains_private_manager_tool,
)


def _marker(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


def redact_agent_message(line, private_item_ids=None) -> str:
    """Remove assistant and advertising payloads from a durable trace line."""
    private_item_ids = private_item_ids if private_item_ids is not None else set()
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _marker({"type": "provider.raw", "redacted": True})
    if not isinstance(payload, dict):
        return _marker({"type": "provider.raw", "redacted": True})

    if str(payload.get("type") or "") in {
        "codex.stderr",
        "silicon.codex_app_error",
    }:
        return _marker({
            "type": str(payload.get("type") or "provider.error"),
            "redacted": True,
        })
    if "error" in payload and not payload.get("method"):
        safe_payload = {"error": {"redacted": True}}
        if "id" in payload:
            safe_payload["id"] = payload["id"]
        return _marker(safe_payload)
    if contains_private_manager_tool(json.dumps(payload, ensure_ascii=False)):
        return _marker({"type": "provider.private", "redacted": True})

    method = str(payload.get("method") or "")
    params = payload.get("params")
    item_id = ""
    item = None
    if isinstance(params, dict):
        item = params.get("item")
        if isinstance(item, dict):
            item_id = str(item.get("id") or params.get("itemId") or "")
        else:
            item_id = str(params.get("itemId") or "")

    params_text = (
        json.dumps(params, ensure_ascii=False)
        if isinstance(params, (dict, list))
        else params
    )
    command_execution = method.startswith("item/commandExecution/") or (
        isinstance(item, dict) and str(item.get("type") or "") == "commandExecution"
    )
    is_private = (
        command_execution
        or method == "error"
        or contains_private_manager_tool(params_text)
        or contains_advertising_memory_reference(params_text)
        or (item_id and item_id in private_item_ids)
    )
    if is_private and item_id:
        private_item_ids.add(item_id)
    if is_private:
        safe_params = {"redacted": True}
        if item_id:
            safe_params["itemId"] = item_id
        if isinstance(item, dict):
            safe_params["item"] = {
                key: item[key]
                for key in ("id", "type", "phase", "status")
                if key in item
            }
            safe_params["item"]["redacted"] = True
        return _marker({"method": method, "params": safe_params})

    if method.startswith("item/agentMessage/"):
        safe_params = {}
        if isinstance(params, dict) and params.get("itemId"):
            safe_params["itemId"] = params["itemId"]
        safe_params["redacted"] = True
        return _marker({"method": method, "params": safe_params})

    if method in {"item/started", "item/completed"} and isinstance(params, dict):
        item = params.get("item")
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            safe_item = {
                key: item[key]
                for key in ("id", "type", "phase", "status")
                if key in item
            }
            safe_item["redacted"] = True
            safe_params = {
                key: value for key, value in params.items() if key != "item"
            }
            safe_params["item"] = safe_item
            return _marker({"method": method, "params": safe_params})

    return line
