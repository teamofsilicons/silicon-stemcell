"""`iwantto trust` — read and change how much this Silicon trusts someone.

Glass is the trust authority. Reading asks Glass for the effective level
(central Carbon, then this Silicon's override, then the team base, then
`very_low`); setting records an override against Glass with the reason. The
history is kept locally, because a decision is only auditable if the reason it
was made is stored next to it.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from iwantto.routing import RoutingError, resolve_target
from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.state import read_json, update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
TRUST_HISTORY_FILE = os.path.join(
    os.fspath(STATE_DIR), "iwantto_trust_history.json"
)

LEVELS = ("very_low", "low", "ok", "high", "very_high", "ultimate")
MAX_HISTORY_PER_TARGET = 200


def _error(message):
    from iwantto.cli import CommandError

    return CommandError(message)


def _default() -> dict:
    return {"version": 1, "history": {}}


def _record(target_key: str, entry: dict) -> None:
    def update(state):
        history = state.setdefault("history", {}).setdefault(target_key, [])
        history.append(entry)
        del history[:-MAX_HISTORY_PER_TARGET]

    try:
        update_json(TRUST_HISTORY_FILE, _default(), update)
    except Exception:
        pass


def _show(target) -> str:
    from interface.trust import inspect_trust_policy

    policy = inspect_trust_policy(
        kind=target.kind,
        public_id=target.fixed_id,
        root=PROJECT_ROOT,
        refresh=True,
    )
    entries = policy.get("entries") if isinstance(policy, dict) else None
    for entry in entries or []:
        if str(entry.get("id") or "") != target.fixed_id:
            continue
        details = [f"effective {entry.get('level')}"]
        if entry.get("base_level"):
            details.append(f"team base {entry['base_level']}")
        if entry.get("override_level"):
            details.append(f"my override {entry['override_level']}")
        details.append(f"source {entry.get('source') or 'default'}")
        if entry.get("central_carbon"):
            details.append("your central carbon")
        return f"{target.label}: " + "; ".join(str(part) for part in details)
    return (
        f"{target.label}: very_low — Glass has no confirmed entry for them, so "
        "they get the default until it does."
    )


def _set(target, level: str, reason: str, actor) -> str:
    from interface import get_contact
    from interface.trust import set_contact_trust

    if level not in LEVELS and level not in {"inherit", "team_default"}:
        raise _error(
            f"{level!r} is not a trust level. Pick one of: {', '.join(LEVELS)} "
            "(or `inherit` to fall back to the team default)."
        )
    if not reason:
        raise _error(
            "Changing trust needs a --reason. Say who confirmed it, and quote "
            "the msgids that back it up."
        )
    initiating = get_contact(actor.contact_id) or {}
    result = set_contact_trust(
        target.kind,
        target.fixed_id,
        None if level in {"inherit", "team_default"} else level,
        reason=reason,
        initiated_by_carbon_id=(
            actor.contact_id
            if initiating.get("contact_type") == "carbon"
            else ""
        ),
        root=PROJECT_ROOT,
    )
    _record(
        f"{target.kind}:{target.fixed_id}",
        {
            "at": time.time(),
            "at_iso": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": result.get("level"),
            "reason": reason,
            "set_by": actor.describe(),
            "revision": result.get("revision"),
        },
    )
    return (
        f"{result.get('target')} is now {result.get('level')} "
        f"(Glass revision {result.get('revision')}). Reason recorded."
    )


def _history(target) -> str:
    state = read_json(TRUST_HISTORY_FILE, _default())
    entries = (state.get("history") or {}).get(f"{target.kind}:{target.fixed_id}")
    if not entries:
        return f"No recorded trust changes for {target.label}."
    lines = [f"Trust history for {target.label}:"]
    for entry in entries:
        lines.append(
            f"  {entry.get('at_iso')} → {entry.get('level')} "
            f"(by {entry.get('set_by')})\n      {entry.get('reason')}"
        )
    return "\n".join(lines)


def cmd_trust(args, actor) -> str:
    try:
        target = resolve_target(
            args.target,
            kind_hint="carbon" if args.carbon else "silicon" if args.silicon else "",
        )
    except RoutingError as exc:
        raise _error(str(exc))

    if args.history:
        return _history(target)
    if args.set:
        return _set(target, str(args.set), str(args.reason or "").strip(), actor)
    return _show(target)


def add_parser(subparsers, parser_cls):
    parser = subparsers.add_parser(
        "trust", help="see or set how far you trust a carbon or silicon"
    )
    parser.add_argument("target", help="carbon id or silicon id")
    parser.add_argument(
        "--set",
        metavar="LEVEL",
        help="new trust level: " + "/".join(LEVELS),
    )
    parser.add_argument("--reason", help="why — required with --set")
    parser.add_argument(
        "--history", action="store_true", help="every trust change so far"
    )
    parser.add_argument("--carbon", action="store_true")
    parser.add_argument("--silicon", action="store_true")
    parser.set_defaults(_handler=cmd_trust)
