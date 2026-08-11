"""One inbound Glass message, dispatched to whatever it asks for."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from interface.agent.commands import execute_command
from interface.agent.config import local_version
from interface.agent.reconcile import (
    TeamContextReconciler,
    TrustPolicyReconciler,
    _request_team_context_reconcile,
    _request_trust_reconcile,
    _team_context_change_reason,
)
from interface.agent.frames import send_json
from interface.agent.terminal import handle_terminal_message

def handle_message(
    ws,
    msg: dict,
    root: Path,
    name: str,
    config: dict | None = None,
    team_context_reconciler: TeamContextReconciler | None = None,
    trust_policy_reconciler: TrustPolicyReconciler | None = None,
) -> None:
    msg_type = msg.get("type")
    if msg_type == "welcome":
        print("[glass-agent] welcome", flush=True)
        return
    if msg_type == "billing":
        print(f"[glass-agent] billing: {msg.get('status') or msg.get('message') or 'ok'}", flush=True)
        return
    if msg_type == "pong":
        return
    if msg_type == "crons.changed":
        try:
            from interface.cron import invalidate_cron_cache

            invalidate_cron_cache()
        except Exception as exc:
            print(
                "[glass-agent] cron invalidation marker deferred: "
                f"{str(exc)[:300]}",
                flush=True,
            )
        return
    if msg_type == "team_context.changed":
        _request_team_context_reconcile(
            team_context_reconciler,
            reason=_team_context_change_reason(msg),
        )
        if str(msg.get("kind") or "") == "team_context":
            # Team membership changes also change the complete effective-trust
            # roster, even when no explicit trust row changed.
            _request_trust_reconcile(
                trust_policy_reconciler,
                force=True,
                reason="websocket-invalidation:trust-roster",
            )
        return
    if msg_type == "trust.changed":
        try:
            from interface.trust import mark_trust_policy_invalidated

            mark_trust_policy_invalidated(
                team_revision=msg.get("team_revision"),
                silicon_revision=msg.get("silicon_revision"),
                root=root,
            )
        except Exception as exc:
            print(
                f"[glass-agent] trust invalidation marker deferred: {str(exc)[:300]}",
                flush=True,
            )
        _request_trust_reconcile(
            trust_policy_reconciler,
            force=True,
            reason="websocket-invalidation:trust",
        )
        return
    if msg_type == "diag.rollup.ack":
        try:
            from diagnostics.push import acknowledge, resolve_db_path

            settings = config or {}
            db_path = resolve_db_path(root, settings.get("diag_db"))
            stored = bool(msg.get("stored"))
            acknowledge(
                db_path,
                msg.get("run_id", ""),
                stored=stored,
                reason=msg.get("reason", ""),
            )
            if not stored:
                rejection = str(msg.get("reason") or "invalid rollup")[:300]
                print(
                    f"[glass-agent] diagnostic rejected run_id={msg.get('run_id', '')}: "
                    f"{rejection}",
                    flush=True,
                )
                send_json(ws, {
                    "type": "log",
                    "level": "error",
                    "source": "diagnostics",
                    "msg": (
                        f"Diagnostic rollup rejected for run "
                        f"{str(msg.get('run_id') or '')[:64]}: {rejection}"
                    ),
                })
        except Exception as exc:
            print(f"[glass-agent] diagnostic ack deferred: {exc}", flush=True)
        return
    if msg_type == "terminal":
        handle_terminal_message(ws, msg, root)
        return
    if msg_type != "command":
        return

    command_id = msg.get("id", "")
    if command_id:
        send_json(ws, {"type": "command_ack", "id": command_id, "command": msg.get("command", "")})
    status, detail = execute_command(msg, root, name)
    # Keep Glass's stored status fresh — the console reads `version` from it
    # (the on-demand "version" command, and any command that may change it).
    status_update = {"type": "status", "version": local_version(root)}
    patch = msg.pop("_status_patch", {})
    if isinstance(patch, dict):
        status_update.update(patch)
    send_json(ws, status_update)
    if command_id:
        send_json(ws, {
            "type": "command_result",
            "id": command_id,
            "command": msg.get("command", ""),
            "status": status,
            "message": detail,
        })
    print(f"[glass-agent] command {msg.get('command')} -> {status}: {detail}", flush=True)
    if msg.pop("_agent_reexec", False):
        print("[glass-agent] re-execing to load updated code", flush=True)
        time.sleep(1)
        os.execv(sys.executable, [sys.executable, "-u", str(Path(__file__).resolve())])


