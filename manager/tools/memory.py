"""Advertising memory: what this Silicon tells its team it can do."""
from manager.tools.base import register
from interface.team_tick import acknowledge_team_context_result

from manager.settings import (
    PROJECT_ROOT,
)


@register(name="advertising_memory/update")
def _tool_advertising_memory_update(tool_spec, carbon_id):
    """Publish this silicon's advertising memory to Glass."""
    content = tool_spec.get("content")
    if not isinstance(content, str):
        return "Tool 'advertising_memory/update': Error: content must be a string"
    resolve_conflict = tool_spec.get("resolve_conflict", False)
    if not isinstance(resolve_conflict, bool):
        return (
            "Tool 'advertising_memory/update': Error: "
            "resolve_conflict must be a boolean"
        )
    try:
        from interface.team import publish as team_publish

        outcome = team_publish.update_own_advertising_memory(
            content,
            root=PROJECT_ROOT,
            resolve_conflict=resolve_conflict,
        )
    except Exception as exc:
        return f"Tool 'advertising_memory/update': Error: {exc}"

    if not isinstance(outcome, dict):
        return f"Tool 'advertising_memory/update': {outcome or 'saved'}"

    if outcome.get("ok") is True:
        acknowledge_team_context_result(outcome)
    status = str(outcome.get("status") or "saved")
    details = []
    revision = outcome.get("revision")
    actual_revision = outcome.get("actual_revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        details.append(f"revision {revision}")
    if isinstance(actual_revision, int) and not isinstance(actual_revision, bool):
        details.append(f"Glass is at revision {actual_revision}")
    if outcome.get("local_saved") and outcome.get("ok") is False:
        details.append("local draft preserved")
    detail = str(outcome.get("detail") or "").strip()
    if detail:
        details.append(detail)
    suffix = f" — {'; '.join(details)}" if details else ""
    error_prefix = "Error: " if outcome.get("ok") is False else ""
    return f"Tool 'advertising_memory/update': {error_prefix}{status}{suffix}"
