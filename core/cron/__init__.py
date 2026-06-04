"""Local execution of Glass-owned cron records plus worker checkbacks."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.cron.checkback import get_checkback_jobs
from core.interface import InterfaceClient, InterfaceError, ensure_contact_for_target

CRON_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CRON_DIR.parents[1]
CHECKBACK_HISTORY_FILE = CRON_DIR / "history.json"
CRON_STATE_FILE = PROJECT_ROOT / "core" / "interface_state" / "crons.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_checkback_history() -> dict[str, Any]:
    return _read_json(CHECKBACK_HISTORY_FILE, {})


def _save_checkback_history(history: dict[str, Any]) -> None:
    _write_json(CHECKBACK_HISTORY_FILE, history)


def _check_checkbacks() -> dict[str, str]:
    history = _load_checkback_history()
    results_by_contact: dict[str, list[str]] = {}

    for job in get_checkback_jobs():
        name = job["name"]
        contact_id = job.get("carbon_id")
        if not contact_id:
            continue
        try:
            should_run = job["trigger"](history.get(name))
        except Exception:
            should_run = False
        if not should_run:
            continue
        try:
            output = job["execute"]()
            if output:
                instructions = job.get("instructions", "")
                parts = [f"Checkback '{name}'"]
                if instructions:
                    parts.append(f"Instructions: {instructions}")
                parts.append(f"Output: {output}")
                results_by_contact.setdefault(contact_id, []).append("\n".join(parts))
            history[name] = {"last_run": time.time()}
            cleanup = job.get("_cleanup")
            if cleanup:
                cleanup()
        except Exception as exc:
            on_error = job.get("on_error")
            if on_error:
                try:
                    on_error(str(exc))
                except Exception:
                    pass
            history[name] = {"last_run": time.time(), "error": str(exc)}

    _save_checkback_history(history)
    return {contact_id: "\n\n".join(parts) for contact_id, parts in results_by_contact.items()}


def _load_cron_state() -> dict[str, Any]:
    state = _read_json(CRON_STATE_FILE, {"version": 1, "crons": {}})
    state.setdefault("version", 1)
    state.setdefault("crons", {})
    return state


def _save_cron_state(state: dict[str, Any]) -> None:
    _write_json(CRON_STATE_FILE, state)


def _cron_id(record: dict[str, Any]) -> str:
    return str(record.get("cron_id") or record.get("id") or "").strip()


def _cron_trigger(record: dict[str, Any]) -> str:
    return str(record.get("trigger") or record.get("schedule") or "").strip()


def _cron_task(record: dict[str, Any]) -> str:
    return str(record.get("task") or record.get("body") or record.get("text") or "").strip()


def _cron_timezone(record: dict[str, Any]) -> str:
    return str(record.get("timezone") or record.get("tz") or "UTC").strip() or "UTC"


def _cron_active(record: dict[str, Any]) -> bool:
    return bool(record.get("active", True))


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _match_part(value: int, expr: str, min_value: int, max_value: int) -> bool:
    expr = expr.strip()
    if expr == "*":
        return True
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            try:
                step = max(int(raw_step), 1)
            except ValueError:
                return False
        if part == "*":
            start, end = min_value, max_value
        elif "-" in part:
            try:
                start, end = [int(x) for x in part.split("-", 1)]
            except ValueError:
                return False
        else:
            try:
                start = end = int(part)
            except ValueError:
                return False
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def _fallback_fire_times(trigger: str, start: datetime, end: datetime, limit: int) -> list[datetime]:
    fields = trigger.split()
    if len(fields) != 5:
        return []
    minute, hour, dom, month, dow = fields
    cursor = start.replace(second=0, microsecond=0)
    cursor = cursor + timedelta(minutes=1)
    fires: list[datetime] = []
    checked = 0
    while cursor <= end and checked < 600_000 and len(fires) < limit:
        cron_dow = (cursor.weekday() + 1) % 7
        dom_match = _match_part(cursor.day, dom, 1, 31)
        dow_match = _match_part(cron_dow, dow, 0, 7)
        day_ok = dom_match if dow == "*" else dow_match if dom == "*" else (dom_match or dow_match)
        if (
            _match_part(cursor.minute, minute, 0, 59)
            and _match_part(cursor.hour, hour, 0, 23)
            and day_ok
            and _match_part(cursor.month, month, 1, 12)
        ):
            fires.append(cursor)
        cursor = cursor + timedelta(minutes=1)
        checked += 1
    return fires


def fire_times_between(trigger: str, watermark: datetime, now: datetime, timezone_name: str = "UTC", limit: int = 250) -> list[datetime]:
    """Return scheduled fire instants after watermark and at/before now."""
    tz = _timezone(timezone_name)
    start_local = watermark.astimezone(tz)
    now_local = now.astimezone(tz)
    if now_local <= start_local:
        return []

    try:
        from croniter import croniter  # type: ignore

        itr = croniter(trigger, start_local)
        fires = []
        while len(fires) < limit:
            nxt = itr.get_next(datetime)
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=tz)
            if nxt > now_local:
                break
            fires.append(nxt.astimezone(timezone.utc))
        return fires
    except Exception:
        return [dt.astimezone(timezone.utc) for dt in _fallback_fire_times(trigger, start_local, now_local, limit)]


def _targets(record: dict[str, Any]) -> list[dict[str, str]]:
    raw_targets = record.get("for_targets") or record.get("targets") or []
    targets: list[dict[str, str]] = []
    if isinstance(raw_targets, dict):
        raw_targets = [raw_targets]
    if isinstance(raw_targets, list):
        for raw in raw_targets:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or raw.get("type") or raw.get("contact_type") or "carbon").lower()
            fixed_id = str(raw.get("id") or raw.get("carbon_id") or raw.get("silicon_id") or "").strip()
            if fixed_id:
                targets.append({"kind": "silicon" if "silicon" in kind else "carbon", "id": fixed_id})
    if not targets and record.get("carbon_id"):
        targets.append({"kind": "carbon", "id": str(record["carbon_id"])})
    if not targets and record.get("silicon_id"):
        targets.append({"kind": "silicon", "id": str(record["silicon_id"])})
    return targets


def _format_cron_context(record: dict[str, Any], fire_dt: datetime, missed_count: int) -> str:
    cron_id = _cron_id(record)
    trigger = _cron_trigger(record)
    timezone_name = _cron_timezone(record)
    task = _cron_task(record)
    missed = missed_count > 1
    lines = [
        "Glass cron fired",
        f"cron_id: {cron_id}",
        f"trigger: {trigger}",
        f"glass_timezone: {timezone_name}",
        f"scheduled_fire_time_utc: {_iso(fire_dt)}",
        f"missed: {str(missed).lower()}",
    ]
    if missed_count > 1:
        lines.append(f"missed_run_count: {missed_count}")
        lines.append("collapsed: true")
    lines.extend(["task:", task])
    return "\n".join(lines)


def _check_glass_crons(now: datetime | None = None, client: InterfaceClient | None = None) -> dict[str, str]:
    now = now or _utc_now()
    client = client or InterfaceClient()
    try:
        payload = client.crons_list()
    except InterfaceError:
        return {}

    records = payload if isinstance(payload, list) else payload.get("crons") or payload.get("data") or payload.get("results") or []
    if not isinstance(records, list):
        return {}

    state = _load_cron_state()
    results: dict[str, list[str]] = {}

    for record in records:
        if not isinstance(record, dict):
            continue
        cron_id = _cron_id(record)
        trigger = _cron_trigger(record)
        if not cron_id or not trigger:
            continue

        entry = state.setdefault("crons", {}).setdefault(cron_id, {})
        entry["last_checked_utc"] = _iso(now)
        if "watermark_utc" not in entry:
            entry["watermark_utc"] = _iso(now)
            entry.setdefault("missed_run_count", 0)
            continue

        if not _cron_active(record):
            continue

        watermark = _parse_iso(entry.get("watermark_utc")) or now
        fires = fire_times_between(trigger, watermark, now, _cron_timezone(record))
        if not fires:
            continue

        latest = fires[-1]
        entry["watermark_utc"] = _iso(latest)
        entry["last_run_utc"] = _iso(latest)
        if len(fires) > 1:
            entry["missed_run_count"] = int(entry.get("missed_run_count") or 0) + len(fires)
        entry["last_result"] = {"status": "queued", "count": len(fires)}

        context = _format_cron_context(record, latest, len(fires))
        for target in _targets(record):
            try:
                contact = ensure_contact_for_target(target["kind"], target["id"], client=client)
            except Exception as exc:
                print(f"[Cron] Could not resolve target {target}: {exc}", flush=True)
                continue
            contact_id = contact.get("silicon_id") if contact.get("contact_type") == "silicon" else contact.get("carbon_id")
            if contact_id:
                results.setdefault(contact_id, []).append(context)

    _save_cron_state(state)
    return {contact_id: "\n\n".join(parts) for contact_id, parts in results.items()}


def check_crons() -> dict[str, str]:
    """Check local worker checkbacks and due Glass cron records."""
    merged: dict[str, list[str]] = {}
    for source in (_check_checkbacks(), _check_glass_crons()):
        for contact_id, context in source.items():
            if context:
                merged.setdefault(contact_id, []).append(context)
    return {contact_id: "\n\n".join(parts) for contact_id, parts in merged.items()}


def execute_cron_tool(tool_spec: dict[str, Any]) -> str:
    tool = tool_spec.get("tool", "")
    client = InterfaceClient()
    if tool == "cron/create":
        trigger = str(tool_spec.get("trigger") or "").strip()
        task = str(tool_spec.get("task") or "").strip()
        targets = tool_spec.get("targets") or []
        if not trigger or not task:
            return "Tool 'cron/create': Error: trigger and task are required"
        if not isinstance(targets, list) or not targets:
            return "Tool 'cron/create': Error: targets is required"
        payload = client.cron_create(trigger, task, targets)
        return "Tool 'cron/create': " + json.dumps(payload, sort_keys=True)

    if tool == "cron/update":
        cron_id = str(tool_spec.get("cron_id") or "").strip()
        if not cron_id:
            return "Tool 'cron/update': Error: cron_id is required"
        payload = client.cron_update(
            cron_id,
            trigger=tool_spec.get("trigger"),
            task=tool_spec.get("task"),
            active=tool_spec.get("active") if "active" in tool_spec else None,
        )
        return "Tool 'cron/update': " + json.dumps(payload, sort_keys=True)

    if tool == "cron/delete":
        cron_id = str(tool_spec.get("cron_id") or "").strip()
        if not cron_id:
            return "Tool 'cron/delete': Error: cron_id is required"
        payload = client.cron_delete(cron_id)
        state = _load_cron_state()
        state.setdefault("crons", {}).pop(cron_id, None)
        _save_cron_state(state)
        return "Tool 'cron/delete': " + json.dumps(payload, sort_keys=True)

    if tool == "cron/list":
        payload = client.crons_list()
        return "Tool 'cron/list': " + json.dumps(payload, sort_keys=True)

    return f"Unknown cron tool: '{tool}'"
