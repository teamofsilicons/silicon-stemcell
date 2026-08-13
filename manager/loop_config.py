"""What the event loop runs, and how often it may fall back to running it.

Native inbox and runtime-file notifications are the primary scheduler. Each
interval here is that handler's recovery ceiling for a platform where a watcher
cannot be installed — not its normal cadence.
"""
from __future__ import annotations

from interface import get_unread_events_durable
from interface.cron import check_crons
from manager.advisor import run_heartbeats as check_advisor_heartbeats
from manager.advisor.heartbeat import check_manager_heartbeats
from iwantto.commands.remember import reap_fired_reminders
from interface.messages import check_manager_messages_durable
from worker import check_completed_workers_formatted
from interface.release.updater import check_for_system_update
from interface.team_tick import check_team_context

LOOP_TICK = 60

EVENT_LOOP = [
    {
        "name": "check_team_context",
        "execute": check_team_context,
        "interval_seconds": 60,
        "jitter_seconds": 15,
        "run_on_startup": True,
    },
    {
        "name": "check_interface",
        "execute": get_unread_events_durable,
        "interval_seconds": 60,
        "jitter_seconds": 15,
        "run_on_activity": True,
        "run_on_startup": True,
    },
    {
        "name": "check_crons",
        "execute": check_crons,
        "interval_seconds": 30,
        "jitter_seconds": 10,
        "run_on_activity": True,
        "run_on_startup": True,
    },
    {
        "name": "check_manager_messages",
        "execute": check_manager_messages_durable,
        "interval_seconds": 60,
        "jitter_seconds": 15,
        "run_on_activity": True,
        "run_on_startup": True,
    },
    {
        "name": "check_system_updates",
        "execute": check_for_system_update,
        "interval_seconds": 60 * 60,
        "jitter_seconds": 5 * 60,
    },
    {
        "name": "check_workers",
        "execute": check_completed_workers_formatted,
        "interval_seconds": 60,
        "jitter_seconds": 15,
        "run_on_activity": True,
        "run_on_startup": True,
    },
    {
        # Checked every minute; the handler decides which managers are due, so
        # the 13-minute cadence survives a restart instead of resetting.
        "name": "check_manager_heartbeats",
        "execute": check_manager_heartbeats,
        "interval_seconds": 60,
        "jitter_seconds": 10,
    },
    {
        "name": "check_advisor_heartbeats",
        "execute": check_advisor_heartbeats,
        "interval_seconds": 5 * 60,
        "jitter_seconds": 60,
    },
    {
        "name": "reap_reminders",
        "execute": reap_fired_reminders,
        "interval_seconds": 5 * 60,
        "jitter_seconds": 30,
    },
]
