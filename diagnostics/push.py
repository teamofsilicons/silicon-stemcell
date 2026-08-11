#!/usr/bin/env python3
"""core/glass_diag_push.py

Sidecar-side emitter for Phase 5: drains unsent diagnostic rollups from the
local rollups.sqlite and hands each one to the Glass agent's live WebSocket as
a `diag.rollup` frame.

WHY THIS LIVES IN THE SIDECAR (glass_agent.py), NOT THE RUN PROCESS
-------------------------------------------------------------------
core/diagnostics.py writes one rollup row per run into rollups.sqlite from the
*agent run* process, which is short-lived and holds no Glass connection. The
glass-agent sidecar is a *separate long-lived process* that already holds the
single authenticated ws/glass/agent/ connection and already reconnects with
backoff. So the natural, fail-open sender is: the sidecar polls sqlite for rows
it has not sent yet and pushes them over its existing socket.

 - Fail-open is structural: a send failure here is in a different process from
   any run, so it cannot delay or break a run. (It can only trip the sidecar's
   own reconnect, which is exactly what we want.)
 - Backfill-on-reconnect is automatic: unsent rows persist in sqlite across
   disconnects and drain on the next connection.

DELIVERY / SENT-STATE
---------------------
Sent-state is tracked in a SEPARATE table, `diag_sent`, owned by this module.
It is deliberately NOT a column on `runs`, because the tracer writes `runs`
with INSERT OR REPLACE and a re-close would clobber a `sent` column. Keeping
sender state in its own table leaves the tracer's write path untouched and
means a re-close never triggers a spurious resend.

Glass v2 replies with `diag.rollup.ack`. The live sidecar uses acknowledgement
mode: a row remains pending until Glass confirms it was stored. A disconnect
between send and acknowledgement therefore causes a safe resend after
reconnect. Compatibility callers may retain the historical mark-on-send mode.
The receiver's ingest is an idempotent upsert on run_id, so duplicates are
harmless. Explicit server rejections are moved to a local dead-letter table so
one malformed row cannot block every later diagnostic forever.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from helpers.paths import resolve_data_relative

log = logging.getLogger("silicon.glass_diag_push")

# Matches core/diagnostics.py's default so the sidecar reads the SAME file the
# tracer writes. See resolve_db_path() for the important cwd/env caveat.
DEFAULT_DIAG_DIR = os.fspath(
    resolve_data_relative(
        os.environ.get("SILICON_DIAG_DIR", "core/interface_state/diagnostics")
    )
)
ABANDONED_TRACE_GRACE_MS = 30_000

# Glass runs uvicorn with --ws-max-size 131072. A frame above that is refused at
# the transport layer with close code 1009 -- the server application never sees
# it, so it can never ack or reject it. Stay under the limit with headroom for
# the WebSocket framing itself.
MAX_FRAME_BYTES = 120_000

# No rollup may block the queue forever. Delivery is counted BEFORE the send and
# committed immediately, so an attempt that kills the socket (or the process)
# still counts. Once a row exhausts its attempts it is dead-lettered locally.
# This is the backstop that makes ANY undeliverable payload self-limiting --
# oversize, malformed, or a server-side fault we have not seen yet -- instead of
# pinning the sidecar in a reconnect loop.
#
# A stored rollup clears its counter on ack, so this only accrues across cycles
# that produced no acknowledgement at all. The budget is set above the couple of
# reconnects a normal Glass deploy causes, so ordinary churn never discards good
# telemetry; sustained silence is what retires a row.
MAX_DELIVERY_ATTEMPTS = 5


def resolve_db_path(root, override=None) -> str:
    """Locate the same rollups.sqlite the tracer writes.

    Priority: explicit override (from .glass.json "diag_db") -> SILICON_DIAG_DIR
    env -> the tracer default, resolved relative to the silicon root when not
    absolute.

    CAVEAT: core/diagnostics.py's default dir is RELATIVE, so the tracer's file
    location depends on the run process's cwd. If the running silicon service
    sets a custom SILICON_DIAG_DIR, or runs with a cwd other than the silicon
    root, confirm this resolves to the SAME file it writes -- otherwise set an
    absolute "diag_db" in .glass.json.
    """
    diag_dir = override or DEFAULT_DIAG_DIR
    if not os.path.isabs(diag_dir):
        diag_dir = os.path.join(str(root), diag_dir)
    return os.path.join(diag_dir, "rollups.sqlite")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # Same busy_timeout the tracer uses; do NOT re-set journal_mode -- WAL is
    # persistent on the file and the tracer already established it.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_sent_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS diag_sent (
            run_id TEXT PRIMARY KEY,
            sent_at_ms INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS diag_rejected (
            run_id TEXT PRIMARY KEY,
            rejected_at_ms INTEGER,
            reason TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS diag_attempts (
            run_id TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_ms INTEGER
        )"""
    )


def acknowledge(db_path: str, run_id: str, *, stored: bool, reason: str = "") -> bool:
    """Persist a Glass acknowledgement. Never raises into the sidecar loop."""
    run_id = str(run_id or "")[:64]
    if not run_id:
        return False
    conn = None
    try:
        conn = _connect(db_path)
        _ensure_sent_table(conn)
        now_ms = int(time.time() * 1000)
        if stored:
            conn.execute(
                "INSERT OR IGNORE INTO diag_sent (run_id, sent_at_ms) VALUES (?, ?)",
                (run_id, now_ms),
            )
            conn.execute("DELETE FROM diag_rejected WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM diag_attempts WHERE run_id = ?", (run_id,))
        else:
            conn.execute(
                """INSERT OR REPLACE INTO diag_rejected
                   (run_id, rejected_at_ms, reason) VALUES (?, ?, ?)""",
                (run_id, now_ms, str(reason or "Glass rejected diagnostic rollup")[:500]),
            )
        conn.commit()
        return True
    except Exception:
        log.exception("glass_diag_push: acknowledgement failed run_id=%s", run_id)
        return False
    finally:
        if conn is not None:
            conn.close()


def _loads(text):
    try:
        return json.loads(text) if text else []
    except Exception:
        return []


def _col(row, name, default=None):
    """Read a column while remaining compatible with pre-v2 rollup databases."""
    try:
        return row[name] if name in row.keys() else default
    except Exception:
        return default


def _row_to_frame(row: sqlite3.Row) -> dict:
    """Build the diag.rollup frame body from a flat `runs` row.

    Mirrors the versioned core/diagnostics.Trace._build_rollup() output shape.

    No silicon identity is included: the receiver resolves Silicon from the
    authenticated socket (self.sid) in consumers.SiliconConnector.
    """
    return {
        "type": "diag.rollup",
        "run_id": row["run_id"],
        "parent_run_id": row["parent_run_id"],
        "trigger": row["trigger"],
        "carbon_id": row["carbon_id"],
        "room_id": _col(row, "room_id", ""),
        "message_ids": _loads(_col(row, "message_ids", "[]")),
        "response_event_ids": _loads(_col(row, "response_event_ids", "[]")),
        "meta": _loads(_col(row, "meta", "{}")),
        "t_start_ms": row["t_start_ms"],
        "t_end_ms": row["t_end_ms"],
        "duration_ms": row["duration_ms"],
        "rounds": row["rounds"],
        "tokens": {
            "input": row["tokens_input"],
            "output": row["tokens_output"],
            "cache_read": row["tokens_cache_read"],
            "cache_creation": row["tokens_cache_creation"],
            "total": row["tokens_total"],
        },
        "cost_usd": row["cost_usd"],
        "provider_calls": row["provider_calls"],
        "workers_spawned": row["workers_spawned"],
        "bottlenecks": _loads(row["bottlenecks"]),
        "spans": _loads(_col(row, "spans", "[]")),
        "events": _loads(_col(row, "events", "[]")),
        "status": row["status"],
        "schema": row["schema"],
        "version": row["version"],
    }


def _partial_rollup(jsonl_path: Path, now_ms: int) -> tuple[dict | None, bool]:
    """Rebuild a best-effort rollup from a closed or abandoned JSONL trace."""
    start = None
    closed_rollup = None
    spans = {}
    span_order = []
    events = []
    observed_ms = 0
    try:
        with jsonl_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    item = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(item, dict):
                    continue
                kind = item.get("kind")
                if kind == "run_start":
                    start = item
                    observed_ms = max(observed_ms, int(item.get("t_start_ms") or 0))
                elif kind == "span_start" and item.get("span_id"):
                    span_id = str(item["span_id"])
                    if span_id not in spans:
                        span_order.append(span_id)
                    spans[span_id] = {
                        "kind": "span_end",
                        "span_id": span_id,
                        "parent_id": item.get("parent_id"),
                        "name": str(item.get("name") or "span"),
                        "path": str(item.get("path") or item.get("name") or "span"),
                        "t_start_ms": int(item.get("t_start_ms") or 0),
                        "t_end_ms": None,
                        "duration_ms": None,
                        "status": "open",
                        "meta": {},
                    }
                    observed_ms = max(observed_ms, spans[span_id]["t_start_ms"])
                elif kind == "span_end" and item.get("span_id"):
                    span_id = str(item["span_id"])
                    if span_id not in spans:
                        span_order.append(span_id)
                    spans[span_id] = {
                        key: item.get(key)
                        for key in (
                            "kind", "span_id", "parent_id", "name", "path",
                            "t_start_ms", "t_end_ms", "duration_ms", "status", "meta",
                        )
                    }
                    observed_ms = max(observed_ms, int(item.get("t_end_ms") or 0))
                elif kind == "event":
                    event = {key: value for key, value in item.items() if key != "kind"}
                    events.append(event)
                    observed_ms = max(observed_ms, int(item.get("t_ms") or 0))
                elif kind == "run_close" and isinstance(item.get("rollup"), dict):
                    closed_rollup = item["rollup"]
    except OSError:
        return None, False

    if closed_rollup is not None:
        return closed_rollup, True
    if not start or not start.get("run_id"):
        return None, False

    file_ms = min(now_ms, int(jsonl_path.stat().st_mtime * 1000))
    end_ms = max(int(start.get("t_start_ms") or 0), observed_ms, file_ms)
    open_count = 0
    for span in spans.values():
        if span.get("t_end_ms") in (None, ""):
            open_count += 1
            span_start = int(span.get("t_start_ms") or end_ms)
            span["t_end_ms"] = end_ms
            span["duration_ms"] = max(0, end_ms - span_start)
            span["status"] = "error"
            span["meta"] = {
                **(span.get("meta") if isinstance(span.get("meta"), dict) else {}),
                "error": "process terminated before span completed",
            }

    meta = start.get("meta") if isinstance(start.get("meta"), dict) else {}
    meta = dict(meta)
    evidence = meta.get("diagnostics_evidence")
    evidence = dict(evidence) if isinstance(evidence, dict) else {}
    evidence.update({
        "recovered_after_process_exit": True,
        "recovery_source": "partial_jsonl",
        "open_spans_recovered": open_count,
    })
    meta["diagnostics_evidence"] = evidence
    events.append({
        "event_id": f"recovered-{str(start['run_id'])[:48]}",
        "parent_id": None,
        "name": "run.recovered_after_process_exit",
        "t_ms": end_ms,
        "meta": {
            "error_summary": "Silicon process ended before diagnostics closed the run",
            "open_spans_recovered": open_count,
        },
    })

    ordered_spans = [spans[span_id] for span_id in span_order]
    tokens = {key: 0 for key in ("input", "output", "cache_read", "cache_creation")}
    cost_usd = 0.0
    provider_calls = 0
    rounds = 0
    for span in ordered_spans:
        if str(span.get("name") or "") == "provider_call":
            provider_calls += 1
            span_meta = span.get("meta") if isinstance(span.get("meta"), dict) else {}
            usage = span_meta.get("tokens") if isinstance(span_meta.get("tokens"), dict) else {}
            for key in tokens:
                try:
                    tokens[key] += max(0, int(usage.get(key) or 0))
                except (TypeError, ValueError, OverflowError):
                    pass
            try:
                cost_usd += max(0.0, float(span_meta.get("cost_usd") or 0))
            except (TypeError, ValueError, OverflowError):
                pass
        if str(span.get("name") or "").startswith("round["):
            rounds += 1
    tokens["total"] = sum(tokens.values())
    response_ids = []
    for event in events:
        event_meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}
        if event.get("name") == "message.egress" and event_meta.get("event_id"):
            response_ids.append(str(event_meta["event_id"])[:64])
    closed_spans = [span for span in ordered_spans if span.get("duration_ms") is not None]
    bottlenecks = [
        {
            "name": str(span.get("path") or span.get("name") or "span"),
            "duration_ms": int(span.get("duration_ms") or 0),
            "pct": round(
                int(span.get("duration_ms") or 0)
                / max(1, end_ms - int(start.get("t_start_ms") or end_ms))
                * 100,
                2,
            ),
        }
        for span in sorted(
            closed_spans,
            key=lambda item: int(item.get("duration_ms") or 0),
            reverse=True,
        )[:5]
    ]
    return {
        "run_id": str(start["run_id"])[:64],
        "parent_run_id": start.get("parent_run_id"),
        "trigger": str(start.get("trigger") or "unknown")[:32],
        "carbon_id": str(start.get("carbon_id") or "")[:64],
        "room_id": str(start.get("room_id") or "")[:64],
        "message_ids": [str(value)[:64] for value in start.get("message_ids", []) if value],
        "response_event_ids": list(dict.fromkeys(response_ids)),
        "meta": meta,
        "t_start_ms": int(start.get("t_start_ms") or end_ms),
        "t_end_ms": end_ms,
        "duration_ms": max(0, end_ms - int(start.get("t_start_ms") or end_ms)),
        "rounds": rounds,
        "tokens": tokens,
        "cost_usd": round(cost_usd, 6),
        "provider_calls": provider_calls,
        "workers_spawned": sum(event.get("name") == "worker.spawned" for event in events),
        "bottlenecks": bottlenecks,
        "spans": ordered_spans,
        "events": events,
        "status": "error",
        "schema": str(start.get("schema") or "silicon.diag"),
        "version": int(start.get("version") or 2),
    }, False


def _persist_recovered_rollup(conn: sqlite3.Connection, rollup: dict, now_ms: int) -> None:
    from diagnostics.store import _ensure_schema

    _ensure_schema(conn)
    tokens = rollup.get("tokens") if isinstance(rollup.get("tokens"), dict) else {}
    conn.execute(
        """INSERT OR IGNORE INTO runs (
            run_id, parent_run_id, trigger, carbon_id, room_id,
            message_ids, response_event_ids, meta,
            t_start_ms, t_end_ms, duration_ms, rounds,
            tokens_input, tokens_output, tokens_cache_read,
            tokens_cache_creation, tokens_total, cost_usd,
            provider_calls, workers_spawned, bottlenecks, spans, events,
            status, schema, version, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rollup["run_id"], rollup.get("parent_run_id"), rollup.get("trigger"),
            rollup.get("carbon_id"), rollup.get("room_id"),
            json.dumps(rollup.get("message_ids", [])),
            json.dumps(rollup.get("response_event_ids", [])),
            json.dumps(rollup.get("meta", {})), rollup.get("t_start_ms"),
            rollup.get("t_end_ms"), rollup.get("duration_ms"), rollup.get("rounds"),
            tokens.get("input", 0), tokens.get("output", 0),
            tokens.get("cache_read", 0), tokens.get("cache_creation", 0),
            tokens.get("total", 0), rollup.get("cost_usd", 0),
            rollup.get("provider_calls", 0), rollup.get("workers_spawned", 0),
            json.dumps(rollup.get("bottlenecks", [])),
            json.dumps(rollup.get("spans", [])), json.dumps(rollup.get("events", [])),
            rollup.get("status", "error"), rollup.get("schema", "silicon.diag"),
            rollup.get("version", 2), now_ms,
        ),
    )


def recover_abandoned_traces(
    db_path: str,
    *,
    current_pid: int | None = None,
    service_running: bool = False,
    now_ms: int | None = None,
    grace_ms: int = ABANDONED_TRACE_GRACE_MS,
) -> int:
    """Persist uploadable failure rollups for traces abandoned by a dead process."""
    now_ms = int(now_ms or time.time() * 1000)
    directory = Path(db_path).parent
    if not directory.is_dir():
        return 0
    recovered = 0
    conn = None
    try:
        conn = _connect(db_path)
        from diagnostics.store import _ensure_schema

        _ensure_schema(conn)
        existing = {
            str(row[0]) for row in conn.execute("SELECT run_id FROM runs").fetchall()
        }
        for path in directory.glob("*.jsonl"):
            if now_ms - int(path.stat().st_mtime * 1000) < max(0, int(grace_ms)):
                continue
            rollup, was_closed = _partial_rollup(path, now_ms)
            if not rollup or rollup["run_id"] in existing:
                continue
            runtime = rollup.get("meta", {}).get("runtime", {})
            source_pid = runtime.get("process_id") if isinstance(runtime, dict) else None
            same_live_process = bool(
                service_running and source_pid and current_pid
                and int(source_pid) == int(current_pid)
            )
            if not was_closed and (same_live_process or (service_running and not source_pid)):
                continue
            _persist_recovered_rollup(conn, rollup, now_ms)
            existing.add(rollup["run_id"])
            recovered += 1
        conn.commit()
        return recovered
    except Exception:
        log.exception("glass_diag_push: abandoned trace recovery failed")
        return recovered
    finally:
        if conn is not None:
            conn.close()


def _frame_bytes(frame: dict) -> int:
    """Serialized size, matching how glass_agent.send_json writes the frame."""
    return len(json.dumps(frame, separators=(",", ":")).encode("utf-8"))


def _shrink_frame(frame: dict, budget: int = MAX_FRAME_BYTES) -> tuple[dict, int]:
    """Return (frame at or under budget, bytes dropped) -- or the original.

    The span/event graph is almost always what makes a rollup oversized, and it
    is also the least important part on the wire: the headline metrics (timing,
    tokens, cost, status, bottlenecks) are what Glass charts. So drop the graph
    rather than the row, and record what was removed so the gap is visible in
    Glass instead of silent.
    """

    original = _frame_bytes(frame)
    if original <= budget:
        return frame, 0

    shrunk = dict(frame)
    dropped_counts = {}
    # Heaviest first: events dwarf spans in practice, so try the cheapest cut.
    for key in ("events", "spans", "bottlenecks"):
        value = shrunk.get(key)
        if not value:
            continue
        dropped_counts[key] = len(value) if isinstance(value, list) else 0
        shrunk[key] = []
        if _frame_bytes(shrunk) <= budget:
            break

    meta = dict(shrunk.get("meta") or {})
    meta["diagnostics_frame_truncated"] = {
        "original_bytes": original,
        "budget_bytes": budget,
        "dropped": dropped_counts,
    }
    shrunk["meta"] = meta
    return shrunk, original - _frame_bytes(shrunk)


def _count_attempt(conn: sqlite3.Connection, run_id: str) -> int:
    """Record a delivery attempt and return the new count, committed up front.

    Committing before the send is the point: if the send closes the socket or
    kills the process, the attempt is still on record, so the next drain sees it
    and the row cannot retry forever.
    """

    conn.execute(
        """INSERT INTO diag_attempts (run_id, attempts, last_attempt_ms)
           VALUES (?, 1, ?)
           ON CONFLICT(run_id) DO UPDATE SET
             attempts = attempts + 1,
             last_attempt_ms = excluded.last_attempt_ms""",
        (run_id, int(time.time() * 1000)),
    )
    conn.commit()
    return int(
        conn.execute(
            "SELECT attempts FROM diag_attempts WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    )


def _dead_letter(conn: sqlite3.Connection, run_id: str, reason: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO diag_rejected (run_id, rejected_at_ms, reason)
           VALUES (?, ?, ?)""",
        (run_id, int(time.time() * 1000), reason[:500]),
    )
    conn.commit()
    log.warning("glass_diag_push: dead-lettered run_id=%s: %s", run_id, reason)


def drain(db_path: str, send_fn, limit: int = 50, *, mark_on_send: bool = True) -> int:
    """Send unsent rollups via send_fn(frame). Return the number sent.

    Contract:
      - sqlite problems are swallowed (fail-open): logged, returns count so far.
      - send_fn exceptions are NOT swallowed: they propagate so the caller can
        treat a broken socket as a disconnect and reconnect.
      - mark_on_send=False is the live Glass v2 contract. Rows remain pending
        until acknowledge() records the server's diag.rollup.ack.
      - `limit` caps how many are sent per call so a large backlog on reconnect
        drains in bounded chunks rather than one burst.
    """
    conn = None
    try:
        conn = _connect(db_path)
    except Exception:
        log.exception("glass_diag_push: cannot open %s", db_path)
        return 0

    sent = 0
    try:
        try:
            _ensure_sent_table(conn)
            rows = conn.execute(
                """SELECT r.* FROM runs r
                   LEFT JOIN diag_sent s ON r.run_id = s.run_id
                   LEFT JOIN diag_rejected x ON r.run_id = x.run_id
                   WHERE s.run_id IS NULL AND x.run_id IS NULL
                   ORDER BY r.created_at ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        except Exception:
            log.exception("glass_diag_push: query for unsent rollups failed")
            return 0

        for row in rows:
            run_id = row["run_id"] if "run_id" in row.keys() else ""
            try:
                frame = _row_to_frame(row)
            except Exception:
                log.exception("glass_diag_push: bad row, skipping run_id=%s", run_id or "?")
                continue

            # Retire anything that has already burned its attempts. This runs
            # before the size check so a row made undeliverable by ANY cause --
            # including one we have not diagnosed -- stops being retried.
            if run_id:
                attempts = _count_attempt(conn, run_id)
                if attempts > MAX_DELIVERY_ATTEMPTS:
                    _dead_letter(
                        conn,
                        run_id,
                        f"undeliverable after {MAX_DELIVERY_ATTEMPTS} attempts",
                    )
                    continue

            # Never hand the socket a frame the server is known to refuse: an
            # oversized frame is closed at the transport with 1009, so it can
            # never be acked or rejected, and would otherwise resend forever.
            frame, dropped = _shrink_frame(frame)
            if dropped:
                log.warning(
                    "glass_diag_push: trimmed %d bytes of span/event graph from run_id=%s",
                    dropped,
                    run_id or "?",
                )
            size = _frame_bytes(frame)
            if size > MAX_FRAME_BYTES:
                _dead_letter(
                    conn,
                    run_id,
                    f"frame is {size} bytes, over the {MAX_FRAME_BYTES}-byte limit",
                )
                continue

            # May raise on a broken socket -- allowed to propagate. Marks for
            # everything already sent this batch are committed below, per row.
            send_fn(frame)

            if mark_on_send:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO diag_sent (run_id, sent_at_ms) VALUES (?, ?)",
                        (row["run_id"], int(time.time() * 1000)),
                    )
                    conn.commit()
                except Exception:
                    # Failing to mark is not fatal: the receiver is idempotent,
                    # so the row simply re-sends next drain.
                    log.exception("glass_diag_push: mark-sent failed run_id=%s",
                                  row["run_id"])
            sent += 1
        return sent
    finally:
        conn.close()
