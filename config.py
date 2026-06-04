from core.interface import get_unread_events
from core.cron import check_crons
from core.messages import check_manager_messages
from worker.handler import check_completed_workers_formatted, clean_old_archives

LOOP_TICK = 10  # Time in seconds between each loop tick
ARCHIVE_FOR = 7 * 24 * 60 * 60  # Time in seconds to keep archived worker states (7 days)

EVENT_LOOP = [
    {
        "name": "check_interface",
        "description": "Check for unread Silicon Interface events",
        "execute": get_unread_events,
        "on_error": lambda e: print(f"[Interface Error] {e}", flush=True),
    },
    {
        "name": "check_crons",
        "description": "Check if any cron jobs need to run",
        "execute": check_crons,
        "on_error": lambda e: print(f"[Cron Error] {e}", flush=True),
    },
    {
        "name": "check_manager_messages",
        "description": "Check for pending inter-manager messages",
        "execute": check_manager_messages,
        "on_error": lambda e: print(f"[Manager Messages Error] {e}", flush=True),
    },
    {
        "name": "check_workers",
        "description": "Check if any workers completed execution",
        "execute": check_completed_workers_formatted,
        "on_error": lambda e: print(f"[Worker Check Error] {e}", flush=True),
    },
    {
        "name": "clean_archives",
        "description": "Remove old worker archives",
        "execute": lambda: clean_old_archives(ARCHIVE_FOR),
        "on_error": lambda e: print(f"[Archive Cleanup Error] {e}", flush=True),
    },
]
