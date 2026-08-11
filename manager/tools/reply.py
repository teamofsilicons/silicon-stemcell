"""The reply tool: what a Carbon actually reads."""
from interface import long_tasks as long_tasks_module
from interface import outbound
from manager.tools.base import register
from manager import activity as activity_module


@register(name="reply")
def _tool_reply(tool_spec, carbon_id):
    """Send the manager's reply. Unless work continues, this closes the task."""
    message = tool_spec.get("message", "")
    work_continues = bool(tool_spec.get("work_continues", False))
    lifecycle = long_tasks_module.current_long_task(carbon_id)
    if lifecycle is not None and not work_continues:
        status = lifecycle.deliver_final_reply(
            message,
            has_active_workers=activity_module._contact_has_active_workers(carbon_id),
            reply_sender=outbound.reply_contact,
        )
    else:
        status = outbound.reply_contact(
            message,
            carbon_id,
            work_continues=work_continues,
        )
    return f"Tool 'reply': {status}"
