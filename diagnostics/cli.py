"""
core/diag_cli.py

Phase 4 deliverable (SILICON_DIAGNOSTICS_TECHNICAL_MEMO, June 20 2026, §5.3):

    python3 main.py diag [--last N | --run <id>]

Read-only local report over the diagnostics artifacts written by
core/diagnostics.py:

    * rollups.sqlite         -- one flattened row per run (query_rollups)
    * <run_id>.jsonl         -- full span detail per run   (read_trace)

For each selected run it prints three sections (memo §5.3):
    1. an indented span timeline with durations,
    2. a ranked bottleneck table,
    3. a token + cost rollup with worker children folded in on
       parent_run_id ("the cost join").

Hard constraints from the memo:
    * READ-ONLY. This command must never write to rollups.sqlite, never
      create the schema, never switch journal mode, and make no network
      calls. It opens SQLite read-only (mode=ro) so a stray write raises
      instead of mutating an operator's DB.
    * Must tolerate a missing JSONL trace. Retention is indefinite by default,
      but an operator can still invoke the explicit finite-policy helper or
      restore an older rollup without its trace. Those cases degrade to
      "trace unavailable; rollup only", never crash.

No diagnostics-layer state is started here; this is a pure reader.
"""

from __future__ import annotations

import os
import sqlite3

from diagnostics.store import (
    DEFAULT_DIAG_DIR,
    read_trace,
)

USAGE = "usage: python3 main.py diag [--last N | --run <id>]"


def _parse_args(argv):
    if not argv:
        return "last", 10, None

    if argv[0] == "--last":
        if len(argv) < 2:
            return None, None, "--last requires a number"
        try:
            n = int(argv[1])
        except ValueError:
            return None, None, f"--last expects an integer, got {argv[1]!r}"
        if n <= 0:
            return None, None, "--last must be positive"
        return "last", n, None

    if argv[0] == "--run":
        if len(argv) < 2:
            return None, None, "--run requires a run id"
        return "run", argv[1], None

    return None, None, f"unrecognized argument {argv[0]!r}"


def _db_path(diag_dir):
    return os.path.join(diag_dir, "rollups.sqlite")


def _connect_ro(db_path):
    if not os.path.exists(db_path):
        return None
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_recent(conn, n):
    cur = conn.execute(
        "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (n,)
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_one(conn, run_id):
    cur = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_children(conn, run_id):
    cur = conn.execute(
        "SELECT * FROM runs WHERE parent_run_id = ? ORDER BY created_at ASC",
        (run_id,),
    )
    return [dict(r) for r in cur.fetchall()]


def _fmt_ms(ms):
    if ms is None:
        return "   --   "
    try:
        return f"{ms / 1000:.3f}s"
    except (TypeError, ValueError):
        return str(ms)


def _fmt_int(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def _fmt_usd(x):
    try:
        return f"${float(x):.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


def _render_timeline(run_id, diag_dir, out):
    jsonl_path = os.path.join(diag_dir, f"{run_id}.jsonl")
    if not os.path.exists(jsonl_path):
        out.append("  timeline: trace file pruned or unavailable; rollup only")
        return

    trace = read_trace(jsonl_path)
    spans = trace.get("spans", {}) or {}
    if not spans:
        out.append("  timeline: no spans recorded")
        return

    children = {}
    roots = []
    for span_id, sp in spans.items():
        pid = sp.get("parent_id")
        if pid and pid in spans:
            children.setdefault(pid, []).append(span_id)
        else:
            roots.append(span_id)

    def walk(span_id, depth):
        sp = spans[span_id]
        name = sp.get("name") or "?"
        dur = sp.get("duration_ms")
        status = sp.get("status") or "ok"
        marker = "" if status == "ok" else f" [{status}]"
        indent = "  " + "  " * depth
        out.append(f"{indent}{name}  {_fmt_ms(dur)}{marker}")
        for child_id in children.get(span_id, []):
            walk(child_id, depth + 1)

    out.append("  timeline:")
    for root_id in roots:
        walk(root_id, 1)


def _render_bottlenecks(rollup, out):
    import json

    raw = rollup.get("bottlenecks")
    items = []
    if raw:
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            items = []

    out.append("  bottlenecks (top spans by absolute duration):")
    if not items:
        out.append("    (none recorded)")
        return
    out.append(f"    {'rank':<5}{'duration':<12}{'pct':<8}span")
    for i, b in enumerate(items, 1):
        name = b.get("name", "?")
        dur = _fmt_ms(b.get("duration_ms"))
        pct = b.get("pct")
        pct_s = f"{pct:.1f}%" if isinstance(pct, (int, float)) else "--"
        out.append(f"    {i:<5}{dur:<12}{pct_s:<8}{name}")


def _sum_tokens(rows):
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}
    cost = 0.0
    for r in rows:
        tot["input"] += int(r.get("tokens_input") or 0)
        tot["output"] += int(r.get("tokens_output") or 0)
        tot["cache_read"] += int(r.get("tokens_cache_read") or 0)
        tot["cache_creation"] += int(r.get("tokens_cache_creation") or 0)
        tot["total"] += int(r.get("tokens_total") or 0)
        try:
            cost += float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
    return tot, cost


def _render_tokens(rollup, children, out):
    own_tokens, own_cost = _sum_tokens([rollup])
    child_tokens, child_cost = _sum_tokens(children)

    combined_total = own_tokens["total"] + child_tokens["total"]
    combined_cost = own_cost + child_cost

    out.append("  tokens & cost:")
    out.append(
        f"    own      : total={_fmt_int(own_tokens['total'])}  "
        f"(in={_fmt_int(own_tokens['input'])} out={_fmt_int(own_tokens['output'])} "
        f"cache_r={_fmt_int(own_tokens['cache_read'])} "
        f"cache_c={_fmt_int(own_tokens['cache_creation'])})  cost={_fmt_usd(own_cost)}"
    )
    if children:
        out.append(
            f"    workers  : total={_fmt_int(child_tokens['total'])}  "
            f"cost={_fmt_usd(child_cost)}  ({len(children)} child run"
            f"{'s' if len(children) != 1 else ''})"
        )
        out.append(
            f"    combined : total={_fmt_int(combined_total)}  "
            f"cost={_fmt_usd(combined_cost)}"
        )
    else:
        out.append("    workers  : none linked (no child runs on parent_run_id)")


def _render_run(conn, rollup, diag_dir, out, full_timeline):
    run_id = rollup.get("run_id", "?")
    trigger = rollup.get("trigger", "?")
    carbon = rollup.get("carbon_id", "?")
    parent = rollup.get("parent_run_id")
    status = rollup.get("status", "?")
    dur = _fmt_ms(rollup.get("duration_ms"))
    rounds = rollup.get("rounds")
    provider_calls = rollup.get("provider_calls")
    workers_spawned = rollup.get("workers_spawned")

    out.append("=" * 72)
    out.append(f"run {run_id}")
    parent_str = parent if parent else "none"
    out.append(
        f"  trigger={trigger}  carbon={carbon}  status={status}  duration={dur}"
    )
    out.append(
        f"  parent={parent_str}  rounds={rounds}  "
        f"provider_calls={provider_calls}  workers_spawned={workers_spawned}"
    )

    children = _fetch_children(conn, run_id)

    if full_timeline:
        _render_timeline(run_id, diag_dir, out)
    _render_bottlenecks(rollup, out)
    _render_tokens(rollup, children, out)


def main(argv, diag_dir=None):
    diag_dir = diag_dir or DEFAULT_DIAG_DIR
    mode, value, err = _parse_args(argv)
    if err:
        print(err)
        print(USAGE)
        return 2

    db_path = _db_path(diag_dir)
    conn = _connect_ro(db_path)
    if conn is None:
        print(f"no diagnostics database found at {db_path}")
        print("(no runs have been recorded yet, or the diag dir differs)")
        return 1

    out = []
    try:
        if mode == "run":
            rollup = _fetch_one(conn, value)
            if rollup is None:
                print(f"run not found: {value}")
                return 1
            _render_run(conn, rollup, diag_dir, out, full_timeline=True)
        else:
            rollups = _fetch_recent(conn, value)
            if not rollups:
                print("no runs recorded yet")
                return 1
            for rollup in rollups:
                _render_run(conn, rollup, diag_dir, out, full_timeline=True)
    finally:
        conn.close()

    print("\n".join(out))
    return 0
