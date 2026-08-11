"""Crons, executed against Glass records."""
from manager.tools.base import register
from interface.cron import execute_cron_tool



@register(prefix="cron/")
def _tool_cron(tool_spec, carbon_id):
    """Run any cron/* tool; interface.cron owns the per-action behaviour."""
    try:
        return execute_cron_tool(tool_spec)
    except Exception as e:
        return f"Tool '{tool_spec.get('tool', '')}': Error: {e}"
