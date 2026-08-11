"""Restarting the service, and picking up where it left off.

A restart is requested by writing a flag; the loop consumes it, replaces the
process, and the flag tells the new one which contact was waiting.
"""
import json
import os
import sys
import time

from manager.settings import (
    RESTART_FLAG,
    RESTART_REQUEST_FILE,
)
from diagnostics.logs import runtime_log as log


def _check_restart_flag():
    """Check if we just restarted. Returns (message, carbon_id_or_None)."""
    if not os.path.exists(RESTART_FLAG):
        return "", None

    try:
        with open(RESTART_FLAG) as f:
            raw = f.read().strip()
        os.remove(RESTART_FLAG)

        # Try JSON format (new)
        try:
            info = json.loads(raw)
            carbon_id = info.get("carbon_id")
            msg = f"Silicon service restarted successfully. {info.get('message', '')}"
            return msg, carbon_id
        except (json.JSONDecodeError, ValueError):
            # Legacy format - just text
            return f"Silicon service restarted successfully. {raw}", None
    except Exception as e:
        try:
            os.remove(RESTART_FLAG)
        except Exception:
            pass
        return f"Silicon service restarted, but error reading restart info: {e}", None


def _check_restart_request():
    """Honour an `iwantto restart-silicon` request left by a manager.

    A command runs in a child process and cannot re-exec the Stemcell, so it
    writes a request here and this loop performs the restart.
    """
    if not os.path.exists(RESTART_REQUEST_FILE):
        return
    carbon_id = None
    try:
        with open(RESTART_REQUEST_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
        carbon_id = payload.get("requested_by") or None
        log(f"[Silicon] Restart requested: {payload.get('reason', '')}")
    except Exception:
        pass
    try:
        os.remove(RESTART_REQUEST_FILE)
    except OSError:
        pass
    error = _do_restart(carbon_id)
    if error:
        log(f"[Silicon] {error}")


def _do_restart(carbon_id=None):
    """Write flag file and re-exec the process."""
    try:
        with open(RESTART_FLAG, "w") as f:
            json.dump({
                "carbon_id": carbon_id,
                "message": f"Restarted at {time.strftime('%Y-%m-%d %H:%M:%S')}",
            }, f)
        log("[Silicon] Restart requested. Re-execing...")
        os.execv(sys.executable, [sys.executable, "-u"] + sys.argv)
    except Exception as e:
        try:
            os.remove(RESTART_FLAG)
        except Exception:
            pass
        return f"Error: restart failed - {e}"
