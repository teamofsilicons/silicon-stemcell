"""The runtime's own voice, for when no brain answered.

This is not something the Silicon writes, which is why it is no longer spelled
as a `reply`. Every configured provider failing, or one needing a login, is a
fact about the machine rather than an answer to anybody — but somebody is
waiting on the other end, so it still has to be said out loud.

It carries no target, because the outage is not addressed to anyone in
particular. It goes to whoever the live turn was answering.
"""
from helpers.silicon import SILICON
from interface import outbound
from manager.tools.base import register


@register(name="brain_error")
def _tool_brain_error(tool_spec, carbon_id):
    message = tool_spec.get("message", "")
    if not message:
        return None
    return f"Tool 'brain_error': {outbound.reply_contact(message, SILICON)}"
