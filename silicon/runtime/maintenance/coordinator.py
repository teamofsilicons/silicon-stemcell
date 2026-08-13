"""The maintenance coordinator, assembled from the four things it does.

Fencing, admitting roots, holding leases, and owing notices are separate
concerns that all have to agree, because they are all decided against the same
document under the same lock. Each is its own class here; the coordinator is
their composition, and the shared transaction lives in
:class:`~silicon.runtime.maintenance.store.MaintenanceStore`.
"""
from __future__ import annotations

from silicon.runtime.maintenance.activities import ActivityLeases
from silicon.runtime.maintenance.drain import DrainControl
from silicon.runtime.maintenance.notices import MaintenanceNotices
from silicon.runtime.maintenance.roots import RootQueue
from silicon.runtime.maintenance.store import MaintenanceStore


class MaintenanceCoordinator(
    DrainControl,
    RootQueue,
    ActivityLeases,
    MaintenanceNotices,
    MaintenanceStore,
):
    """One coordinator, one document, one lock."""
