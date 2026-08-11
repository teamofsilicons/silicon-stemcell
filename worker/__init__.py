"""Workers: one detached run against a provider, owned by one contact.

Three kinds — browser, terminal, writer — differing only where they have to.
Importing this package registers all three, so ``Worker.resolve(kind, ...)``
can find them.

    base        the Worker ABC and its registry
    browser     the shared profile, its queue, and incognito cleanup
    terminal    must already be registered; forgets itself on launch failure
    writer      the base lifecycle, unchanged
    constants   paths and tunables
    registry    the three JSON documents and the archives
    process     environment, launch, feeder, termination
    pool        the queue drain, the completion sweep, the listings
    leases      the one place worker/ talks to maintenance

Nothing at this level imports interface/ or manager/: interface.cron.checkback
imports this package at module scope, so those must stay lazy and in-function.
"""
from worker.base import Worker
from worker.browser import BrowserWorker, sweep_orphaned_daemons
from worker.constants import (
    ACTIVE_FILE,
    ARCHIVE_META_FILE,
    BROWSER_QUEUE_FILE,
    OUTPUTS_DIR,
    SILICON_BROWSER_PROFILE,
    WORKER_REGISTRY_FILE,
    WORKER_STATE_DIR,
    WORKSPACE_ROOT,
)
from worker.dispatch import reconcile_maintenance_activities
from worker.pool import (
    get_worker_status,
    list_active,
    list_archive,
    message_worker,
    read_archive,
    start_worker,
    stop_worker,
)
from worker.sweep import (
    check_completed_workers,
    check_completed_workers_formatted,
)
from worker.terminal import TerminalWorker
from worker.writer import WriterWorker

__all__ = [
    "ACTIVE_FILE",
    "ARCHIVE_META_FILE",
    "BROWSER_QUEUE_FILE",
    "BrowserWorker",
    "OUTPUTS_DIR",
    "SILICON_BROWSER_PROFILE",
    "TerminalWorker",
    "WORKER_REGISTRY_FILE",
    "WORKER_STATE_DIR",
    "WORKSPACE_ROOT",
    "Worker",
    "WriterWorker",
    "check_completed_workers",
    "check_completed_workers_formatted",
    "get_worker_status",
    "list_active",
    "list_archive",
    "message_worker",
    "read_archive",
    "reconcile_maintenance_activities",
    "start_worker",
    "stop_worker",
    "sweep_orphaned_daemons",
]
