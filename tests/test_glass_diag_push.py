"""tests/test_glass_diag_push.py

Unit tests for the Phase 5 sidecar sender (core/glass_diag_push).

Covers the sender's OWN logic -- schema-accurate reads, correct frame shape
against the ingest contract, oldest-first ordering, mark-sent / no-resend,
incremental backfill, mid-batch send-failure recovery, batch limit, and
fail-open behaviour. No live Glass or WS stack required: `send` is a plain
callback, so a broken socket is modelled by a callback that raises.

The seed helpers below mirror core/diagnostics._ensure_schema() and
_write_rollup_row() so the sender is exercised against a byte-accurate replica
of what the tracer actually writes to rollups.sqlite. If the tracer's `runs`
schema or written column set ever changes, update _create_runs_schema /
_seed_run here to match (that divergence is exactly what this test should
surface).

Run: python3 -m unittest tests.test_glass_diag_push
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

from core.glass_diag_push import (
    _row_to_frame,
    acknowledge,
    drain,
    recover_abandoned_traces,
    resolve_db_path,
)


def _create_runs_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, parent_run_id TEXT, trigger TEXT,
            carbon_id TEXT, t_start_ms INTEGER, t_end_ms INTEGER,
            duration_ms INTEGER, rounds INTEGER, tokens_input INTEGER,
            tokens_output INTEGER, tokens_cache_read INTEGER,
            tokens_cache_creation INTEGER, tokens_total INTEGER, cost_usd REAL,
            provider_calls INTEGER, workers_spawned INTEGER, bottlenecks TEXT,
            status TEXT, schema TEXT, version INTEGER, created_at INTEGER
        )"""
    )
    conn.commit()


def _seed_run(conn, run_id, created_at, *, trigger="manager_loop",
              parent_run_id=None, carbon_id="c1", tokens=None, cost_usd=0.0,
              bottlenecks=None, status="ok"):
    tokens = tokens or {}
    conn.execute(
        """INSERT OR REPLACE INTO runs (
            run_id, parent_run_id, trigger, carbon_id, t_start_ms, t_end_ms,
            duration_ms, rounds, tokens_input, tokens_output, tokens_cache_read,
            tokens_cache_creation, tokens_total, cost_usd, provider_calls,
            workers_spawned, bottlenecks, status, schema, version, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, parent_run_id, trigger, carbon_id, 1000, 2000, 1000, 1,
            tokens.get("input", 0), tokens.get("output", 0),
            tokens.get("cache_read", 0), tokens.get("cache_creation", 0),
            tokens.get("total", 0), cost_usd, 1, 0,
            json.dumps(bottlenecks or []), status, "silicon.diag", 1, created_at,
        ),
    )
    conn.commit()


class GlassDiagPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "rollups.sqlite")
        conn = sqlite3.connect(self.db)
        _create_runs_schema(conn)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self):
        return sqlite3.connect(self.db)

    def test_frame_shape_matches_ingest_contract(self):
        conn = self._conn()
        _seed_run(conn, "r1", 1,
                  tokens={"input": 500, "output": 100, "cache_read": 0,
                          "cache_creation": 0, "total": 600},
                  cost_usd=0.01,
                  bottlenecks=[{"name": "round[0]", "duration_ms": 5, "pct": 1.0}])
        conn.close()

        sent = []
        n = drain(self.db, sent.append)
        self.assertEqual(n, 1)
        f = sent[0]
        self.assertEqual(f["type"], "diag.rollup")
        self.assertEqual(f["schema"], "silicon.diag")
        self.assertEqual(f["version"], 1)
        self.assertEqual(f["tokens"], {"input": 500, "output": 100,
                                       "cache_read": 0, "cache_creation": 0,
                                       "total": 600})
        self.assertEqual(f["bottlenecks"],
                         [{"name": "round[0]", "duration_ms": 5, "pct": 1.0}])
        self.assertEqual(f["run_id"], "r1")
        self.assertEqual(f["trigger"], "manager_loop")
        self.assertEqual(f["cost_usd"], 0.01)
        blob = json.dumps(f)
        self.assertNotIn("silicon_key", blob)
        self.assertNotIn("silicon_id", blob)

    def test_sends_all_unsent_oldest_first(self):
        conn = self._conn()
        _seed_run(conn, "a", 100)
        _seed_run(conn, "b", 200)
        _seed_run(conn, "c", 300)
        conn.close()
        sent = []
        self.assertEqual(drain(self.db, sent.append), 3)
        self.assertEqual([f["run_id"] for f in sent], ["a", "b", "c"])

    def test_second_drain_sends_nothing(self):
        conn = self._conn()
        _seed_run(conn, "a", 1)
        conn.close()
        self.assertEqual(drain(self.db, lambda f: None), 1)
        second = []
        self.assertEqual(drain(self.db, second.append), 0)
        self.assertEqual(second, [])

    def test_only_new_unsent_row_is_sent(self):
        conn = self._conn()
        _seed_run(conn, "a", 1)
        conn.close()
        self.assertEqual(drain(self.db, lambda f: None), 1)
        conn = self._conn()
        _seed_run(conn, "b", 2)
        conn.close()
        sent = []
        self.assertEqual(drain(self.db, sent.append), 1)
        self.assertEqual([f["run_id"] for f in sent], ["b"])

    def test_send_failure_propagates_and_remainder_retried(self):
        conn = self._conn()
        for i, rid in enumerate(["r1", "r2", "r3"]):
            _seed_run(conn, rid, i)
        conn.close()

        calls = {"n": 0}
        delivered = []

        def flaky(frame):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ConnectionError("socket dropped")
            delivered.append(frame["run_id"])

        with self.assertRaises(ConnectionError):
            drain(self.db, flaky)
        self.assertEqual(delivered, ["r1"])

        resumed = []
        self.assertEqual(drain(self.db, lambda f: resumed.append(f["run_id"])), 2)
        self.assertEqual(resumed, ["r2", "r3"])

    def test_limit_bounds_batch(self):
        conn = self._conn()
        for i in range(5):
            _seed_run(conn, f"r{i}", i)
        conn.close()
        sent = []
        self.assertEqual(drain(self.db, sent.append, limit=2), 2)
        self.assertEqual([f["run_id"] for f in sent], ["r0", "r1"])

    def test_acknowledgement_mode_retries_until_glass_confirms_storage(self):
        conn = self._conn()
        _seed_run(conn, "ack-me", 1)
        conn.close()

        first = []
        self.assertEqual(drain(self.db, first.append, mark_on_send=False), 1)
        second = []
        self.assertEqual(drain(self.db, second.append, mark_on_send=False), 1)
        self.assertEqual([item["run_id"] for item in second], ["ack-me"])

        self.assertTrue(acknowledge(self.db, "ack-me", stored=True))
        self.assertEqual(drain(self.db, lambda frame: None, mark_on_send=False), 0)

    def test_explicit_rejection_is_dead_lettered_without_blocking_later_runs(self):
        conn = self._conn()
        _seed_run(conn, "bad", 1)
        _seed_run(conn, "good", 2)
        conn.close()

        self.assertTrue(acknowledge(
            self.db,
            "bad",
            stored=False,
            reason="invalid graph",
        ))
        sent = []
        self.assertEqual(drain(self.db, sent.append, mark_on_send=False), 1)
        self.assertEqual([item["run_id"] for item in sent], ["good"])
        conn = self._conn()
        reason = conn.execute(
            "SELECT reason FROM diag_rejected WHERE run_id='bad'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(reason, "invalid graph")

    def test_abandoned_jsonl_is_recovered_as_an_uploadable_failed_run(self):
        trace_path = os.path.join(self.tmp, "orphan.jsonl")
        records = [
            {
                "kind": "run_start",
                "run_id": "orphan",
                "parent_run_id": None,
                "trigger": "message",
                "carbon_id": "carbon-1",
                "room_id": "room-1",
                "message_ids": ["event-in"],
                "meta": {"runtime": {"process_id": 111}},
                "t_start_ms": 1_000,
                "schema": "silicon.diag",
                "version": 2,
            },
            {
                "kind": "span_start",
                "span_id": "round",
                "parent_id": None,
                "name": "round[0]",
                "path": "round[0]",
                "t_start_ms": 1_100,
            },
            {
                "kind": "span_start",
                "span_id": "provider",
                "parent_id": "round",
                "name": "provider_call",
                "path": "round[0]>provider_call",
                "t_start_ms": 1_200,
            },
            {
                "kind": "span_end",
                "span_id": "provider",
                "parent_id": "round",
                "name": "provider_call",
                "path": "round[0]>provider_call",
                "t_start_ms": 1_200,
                "t_end_ms": 1_500,
                "duration_ms": 300,
                "status": "ok",
                "meta": {
                    "provider": "claude",
                    "tokens": {"input": 10, "output": 2},
                    "cost_usd": 0.01,
                },
            },
        ]
        with open(trace_path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        os.utime(trace_path, (1, 1))

        self.assertEqual(recover_abandoned_traces(
            self.db,
            current_pid=222,
            service_running=True,
            now_ms=2_000_000,
            grace_ms=0,
        ), 1)

        conn = self._conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM runs WHERE run_id='orphan'").fetchone()
        conn.close()
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["provider_calls"], 1)
        self.assertEqual(row["tokens_total"], 12)
        self.assertTrue(json.loads(row["meta"])["diagnostics_evidence"]["recovered_after_process_exit"])
        spans = json.loads(row["spans"])
        self.assertEqual(next(item for item in spans if item["span_id"] == "round")["status"], "error")

        sent = []
        self.assertEqual(drain(self.db, sent.append), 1)
        self.assertEqual(sent[0]["run_id"], "orphan")

    def test_missing_table_is_fail_open(self):
        broken = os.path.join(self.tmp, "no_runs_table.sqlite")
        sqlite3.connect(broken).close()
        self.assertEqual(drain(broken, lambda f: None), 0)

    def test_resolve_db_path_absolute_override(self):
        self.assertEqual(
            resolve_db_path("/some/root", "/abs/diag"),
            os.path.join("/abs/diag", "rollups.sqlite"),
        )

    def test_resolve_db_path_relative_is_joined_to_root(self):
        self.assertEqual(
            resolve_db_path("/some/root", "reldir"),
            os.path.join("/some/root", "reldir", "rollups.sqlite"),
        )


if __name__ == "__main__":
    unittest.main()
