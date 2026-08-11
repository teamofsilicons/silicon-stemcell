"""Messaging another manager, Carbon or Silicon."""
from interface.long_tasks import registry as lt_registry
from manager.tools.base import register
from manager.tools.helpers import _call_preparation_failure_status
from manager.tools.helpers import _message_failure_status
from manager.tools.helpers import _work_reference_suffix
from interface import (
    ensure_contact_for_target,
)
from interface.messages import send_manager_message
from interface.work_updates import (
    prepare_outbound_call,
)


@register(name="message_manager")
def _tool_message_manager(tool_spec, carbon_id):
    """Relay a message to another carbon's or silicon's manager as a work call."""
    message = tool_spec.get("message", "")
    if not message:
        return "Tool 'message_manager': Error: message is required"

    # Carbons are addressed through their manager; silicons by their own name.
    target_carbon_id = tool_spec.get("carbon_id", "")
    target_silicon_id = tool_spec.get("silicon_id", "")
    if target_carbon_id:
        contact_type, requested_id, target_kind = "carbon", target_carbon_id, "manager"
    elif target_silicon_id:
        contact_type, requested_id, target_kind = "silicon", target_silicon_id, "silicon"
    else:
        return "Tool 'message_manager': Error: carbon_id or silicon_id is required"

    lifecycle = lt_registry.current_long_task(carbon_id)
    call_task_id = (
        lifecycle.resolve_task_id(str(tool_spec.get("task_id") or ""))
        if lifecycle is not None
        else str(tool_spec.get("task_id") or "")
    )

    try:
        contact = ensure_contact_for_target(contact_type, requested_id)
    except Exception as e:
        status = _message_failure_status(carbon_id, contact_type, requested_id, e)
        return f"Tool 'message_manager' (to {requested_id}): Error: {status}"

    target_id = contact.get(f"{contact_type}_id") or requested_id
    display = contact.get("display_name") or contact.get("name") or target_id
    target_name = f"{display}'s manager" if contact_type == "carbon" else str(display)

    try:
        work_call = prepare_outbound_call(
            carbon_id,
            target_kind=target_kind,
            target_id=target_id,
            target_name=target_name,
            message=message,
            task_id=call_task_id,
        )
    except Exception as exc:
        status = _call_preparation_failure_status(
            carbon_id,
            contact_type,
            target_id,
            exc,
        )
        return f"Tool 'message_manager' (to {target_id}): Error: {status}"

    status = send_manager_message(
        carbon_id,
        target_id,
        message,
        target_type=contact_type,
        work_call=work_call,
    )
    return (
        f"Tool 'message_manager' (to {target_id}): {status}"
        + _work_reference_suffix(
            work_call,
            "task_id",
            "work_event_id",
            "call_id",
        )
    )
