"""The work_update tool: the durable card a Carbon watches."""
from interface.long_tasks import registry as lt_registry
from silicon.tools.base import register
from interface.work import (
    execute_work_update,
)


@register(name="work_update")
def _tool_work_update(tool_spec, carbon_id):
    """Record progress against the carbon's open long-running task."""
    lifecycle = lt_registry.current_long_task(carbon_id)
    prepared = (
        lifecycle.prepare_work_update(tool_spec)
        if lifecycle is not None
        else [tool_spec]
    )
    results = [
        execute_work_update(prepared_spec, carbon_id)
        for prepared_spec in prepared
    ]
    if lifecycle is not None:
        lifecycle.record_work_update(tool_spec, prepared, results)
    return "Tool 'work_update': " + " ".join(str(result) for result in results)
