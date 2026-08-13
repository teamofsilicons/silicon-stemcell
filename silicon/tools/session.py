"""Resetting a conversation, and restarting the service."""
from silicon.tools.base import register
from silicon import (
    new_session,
)



@register(name="new_session")
def _tool_new_session(tool_spec, carbon_id):
    """Start a fresh manager session, dropping the current conversation."""
    return f"Tool 'new_session': Done. New session id: {new_session(carbon_id)}"


@register(name="restart_silicon_service")
def _tool_restart_silicon_service(tool_spec, carbon_id):
    """No-op here: execute_all_tools performs the restart after the batch."""
    return None
