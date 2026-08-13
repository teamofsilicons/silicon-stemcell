"""Handing a Carbon a live browser, and taking a message back."""
from silicon.tools.base import register
from interface import (
    complete_take_back,
    remote_browser_close,
    remote_browser_share,
    take_back_event,
)



@register(name="remote_browser")
def _tool_remote_browser(tool_spec, carbon_id):
    """Share or close the carbon-visible remote browser session."""
    action_type = tool_spec.get("type", "share")
    if action_type == "share":
        expiry = tool_spec.get("expiry", 60)
        new = tool_spec.get("new", True)
        start_url = tool_spec.get("url") or tool_spec.get("start_url") or ""
        status = remote_browser_share(carbon_id, expiry=expiry, new=new, url=start_url)
        return f"Tool 'remote_browser/share': {status}"
    if action_type == "close":
        status = remote_browser_close(carbon_id)
        return f"Tool 'remote_browser/close': {status}"
    return f"Tool 'remote_browser': Unknown type '{action_type}'"


@register(name="take_back")
def _tool_take_back(tool_spec, carbon_id):
    """Complete a pending take-back request, or retract an already-sent event."""
    request_id = tool_spec.get("request_id", "")
    event_id = tool_spec.get("event_id", "")
    if request_id:
        status = complete_take_back(request_id, tool_spec.get("message", ""))
        return f"Tool 'take_back': {status}"
    if event_id:
        status = take_back_event(
            event_id,
            reason=tool_spec.get("reason", ""),
            force=bool(tool_spec.get("force", False)),
        )
        return f"Tool 'take_back': {status}"
    return "Tool 'take_back': Error: request_id or event_id is required"
