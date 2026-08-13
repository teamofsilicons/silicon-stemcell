"""The event loop's schedule, and one pass over it.

Native inbox and runtime-file notifications are the primary scheduler; the
interval on each handler is its recovery ceiling, not its normal cadence.
"""
from interface import outbound
import hashlib
import re
import time
from helpers.session import SILICON
from silicon.loop_config import EVENT_LOOP, LOOP_TICK
from silicon import (
    new_session,
)
from diagnostics.store import Diagnostics

from silicon.settings import (
    PROJECT_ROOT,
)
from diagnostics.logs import runtime_log as log


COMMAND_RE = re.compile(r"\[COMMAND: (NEW_SESSION|START) from ([^\]]+)\]")


def handle_commands(context_by_carbon):
    """Answer /new and /start, and return what is left to run a turn on.

    A command names the contact who typed it rather than arriving with a sender
    envelope, because it is runtime control and its acknowledgement is the whole
    of it — there is nothing here for the session to think about.
    """
    cleaned = {}
    for carbon_id, context in context_by_carbon.items():
        for match in COMMAND_RE.finditer(context):
            name, sender = match.group(1), match.group(2)
            if name == "NEW_SESSION":
                new_id = new_session(carbon_id)
                log(f"[Silicon] New session at {sender}'s request: {new_id}")
                outbound.reply_contact(
                    "New session started. Fresh context loaded.", sender
                )
            else:
                outbound.reply_contact("Silicon is online and ready.", sender)
        context = COMMAND_RE.sub("", context).strip()

        if context:
            cleaned[carbon_id] = context
        else:
            trace = Diagnostics.get_active_run(carbon_id)
            if trace is not None:
                Diagnostics.unregister_active(carbon_id, trace)
                trace.close()
    return cleaned


def run_event_loop_tick(handler_names=None):
    """Run selected event handlers. Returns ``{SILICON: context_string}``.

    Handlers still answer per contact, because a message, a cron and a worker
    result each belong to somebody. There is only one session to hand them to,
    so they are merged into one root here. Who each part came from travels in
    the text — see :func:`helpers.session.envelope` — which is what lets a
    reply and a progress frame find their room again after a restart.
    """
    parts = []
    selected = None if handler_names is None else set(handler_names)

    for handler in EVENT_LOOP:
        if selected is not None and handler["name"] not in selected:
            continue
        try:
            result = handler["execute"]()
            if not result:
                continue

            if isinstance(result, dict):
                parts.extend(ctx for ctx in result.values() if ctx)
            elif isinstance(result, str) and result:
                log(f"[Silicon] Warning: handler '{handler['name']}' returned string instead of dict")

        except Exception as e:
            log(f"[Silicon] Error in {handler['name']}: {e}")

    return {SILICON: "\n\n".join(parts)} if parts else {}


class EventLoopSchedule:
    """Independent, deterministic recovery clocks for event-loop handlers."""

    def __init__(self, handlers, *, now=None, identity=""):
        self.handlers = {handler["name"]: handler for handler in handlers}
        self.identity = str(identity or PROJECT_ROOT)
        self.attempts = {name: 0 for name in self.handlers}
        current = time.monotonic() if now is None else float(now)
        self.next_due = {}
        for name, handler in self.handlers.items():
            if handler.get("run_on_startup"):
                self.next_due[name] = current
            else:
                self.next_due[name] = current + self._delay(name, handler)

    def _delay(self, name, handler):
        interval = max(0.1, float(handler.get("interval_seconds") or LOOP_TICK))
        jitter = max(0.0, float(handler.get("jitter_seconds") or 0.0))
        attempt = self.attempts[name]
        digest = hashlib.sha256(
            f"{self.identity}:{name}:{attempt}".encode("utf-8")
        ).digest()
        fraction = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
        return interval + (jitter * fraction)

    def due(self, now, *, activity=False, eligible=None):
        allowed = set(self.handlers) if eligible is None else set(eligible)
        names = {
            name
            for name in allowed
            if name in self.next_due and now >= self.next_due[name]
        }
        if activity:
            names.update(
                name
                for name in allowed
                if self.handlers.get(name, {}).get("run_on_activity")
            )
        return names

    def record_attempts(self, names, now):
        for name in names:
            deadline = self.next_due.get(name)
            # An event-triggered attempt must not postpone the independent
            # recovery clock unless that clock was itself due.
            if deadline is None or now < deadline:
                continue
            self.attempts[name] += 1
            self.next_due[name] = now + self._delay(name, self.handlers[name])

    def seconds_until_due(self, now, *, eligible=None):
        allowed = set(self.handlers) if eligible is None else set(eligible)
        deadlines = [
            deadline
            for name, deadline in self.next_due.items()
            if name in allowed
        ]
        if not deadlines:
            return float(LOOP_TICK)
        return max(0.0, min(deadlines) - now)
