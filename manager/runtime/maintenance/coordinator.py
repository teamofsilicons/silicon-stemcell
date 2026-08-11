"""The maintenance coordinator, assembled from the four things it does.

Fencing, admitting roots, holding leases, and owing notices are separate
concerns that all have to agree, because they are all decided against the same
document under the same lock. Each is its own class here; the coordinator is
their composition, and the shared transaction lives in
:class:`~manager.runtime.maintenance.store.MaintenanceStore`.
"""
from __future__ import annotations

from manager.runtime.maintenance.activities import ActivityLeases
from manager.runtime.maintenance.drain import DrainControl
from manager.runtime.maintenance.notices import MaintenanceNotices
from manager.runtime.maintenance.roots import RootQueue
from manager.runtime.maintenance.store import MaintenanceStore


class MaintenanceCoordinator(
    DrainControl,
    RootQueue,
    ActivityLeases,
    MaintenanceNotices,
    MaintenanceStore,
):
    """One coordinator, one document, one lock."""
