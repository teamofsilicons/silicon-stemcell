"""Retired user cron module.

User crons are Glass records managed through Silicon Interface:

    si --json crons list --mine

Stemcell executes those records locally from core/cron/__init__.py and keeps
watermarks in core/interface_state/crons.json.

Worker checkbacks are still local one-shot operational timers.
"""

JOBS = []
