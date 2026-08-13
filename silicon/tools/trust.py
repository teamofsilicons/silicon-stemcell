"""Reading and changing trust. Glass stays the authority."""
from silicon.tools.base import register
import json
from interface import (
    get_contact,
)

from silicon.settings import (
    PROJECT_ROOT,
)


@register(name="trust/list")
@register(name="trust/get")
def _tool_trust_inspect(tool_spec, carbon_id):
    """Read the effective trust policy for one contact, or list every contact."""
    tool_name = tool_spec.get("tool", "")
    target_carbon_id = str(tool_spec.get("carbon_id") or "").strip()
    target_silicon_id = str(tool_spec.get("silicon_id") or "").strip()
    if tool_name == "trust/get" and (
        bool(target_carbon_id) == bool(target_silicon_id)
    ):
        return (
            "Tool 'trust/get': Error: provide exactly one of carbon_id "
            "or silicon_id"
        )
    if tool_name == "trust/list" and target_carbon_id and target_silicon_id:
        return (
            "Tool 'trust/list': Error: provide at most one of carbon_id "
            "or silicon_id"
        )
    try:
        from interface.trust import inspect_trust_policy

        policy = inspect_trust_policy(
            kind=(
                "carbon"
                if target_carbon_id
                else "silicon"
                if target_silicon_id
                else ""
            ),
            public_id=target_carbon_id or target_silicon_id,
            root=PROJECT_ROOT,
            refresh=True,
        )
    except Exception as exc:
        return f"Tool '{tool_name}': Error: {exc}"
    return f"Tool '{tool_name}': {json.dumps(policy, sort_keys=True)}"


@register(name="trust/set")
def _tool_trust_set(tool_spec, carbon_id):
    """Change one contact's trust level, recording who initiated the change."""
    target_carbon_id = str(tool_spec.get("carbon_id") or "").strip()
    target_silicon_id = str(tool_spec.get("silicon_id") or "").strip()
    if bool(target_carbon_id) == bool(target_silicon_id):
        return (
            "Tool 'trust/set': Error: provide exactly one of carbon_id "
            "or silicon_id"
        )
    raw_level = tool_spec.get("level")
    # An empty/inherit level clears the override and falls back to the team default.
    level = None if raw_level in {None, "", "inherit", "team_default"} else str(raw_level)
    try:
        from interface.trust import set_contact_trust

        initiating_contact = get_contact(carbon_id) or {}
        result = set_contact_trust(
            "carbon" if target_carbon_id else "silicon",
            target_carbon_id or target_silicon_id,
            level,
            reason=str(tool_spec.get("reason") or ""),
            initiated_by_carbon_id=(
                carbon_id
                if initiating_contact.get("contact_type") == "carbon"
                else ""
            ),
            root=PROJECT_ROOT,
        )
    except Exception as exc:
        return f"Tool 'trust/set': Error: {exc}"
    return (
        f"Tool 'trust/set': {result['target']} is now "
        f"{result['level']} at Glass revision {result['revision']}"
    )
