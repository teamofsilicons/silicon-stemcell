"""
tests/test_diagnostics.py

Phase 1 tracer unit tests (memo Section 10.1). Pure stdlib unittest, no
external dependencies. Covers, one test class per requirement:

  10.1.a  span nesting + absolute duration accuracy
  10.1.b  bottleneck ranking order + pct computation
  10.1.c  token summation across multiple provider_call spans / multi-round
  10.1.d  fail-open: a span exception does not prevent trace.close()
  10.1.e  contextvar isolation: concurrent runs on separate threads
  10.1.f  unclosed-run tolerance: partial JSONL is parseable

Deterministic timing tests drive a fake monotonic clock so durations are exact
and never flaky on a loaded CI box.
"""

import os
import sys
import json
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diagnostics import store as D  # noqa: E402


class FakeClock:
    """Controllable monotonic clock. Advance with .tick(ms)."""

    def __init__(self):
        self.ns = 0

    def mono_ns(self):
        return self.ns

    def wall_ms(self):
        return self.ns // 1_000_000

    def tick(self, ms):
        self.ns += ms * 1_000_000


class _ClockMixin:
    def install_clock(self):
        self.clock = FakeClock()
        self._orig_mono, self._orig_wall = D._mono_ns, D._wall_ms
        D._mono_ns = self.clock.mono_ns
        D._wall_ms = self.clock.wall_ms
        self.addCleanup(self._restore_clock)

    def _restore_clock(self):
        D._mono_ns, D._wall_ms = self._orig_mono, self._orig_wall

    def tmpdir(self):
        d = tempfile.mkdtemp(prefix="diag_test_")
        return d


class TestSpanNestingAndDuration(_ClockMixin, unittest.TestCase):
    """10.1.a -- nesting hierarchy + absolute duration computation."""

    def test_nested_durations_and_paths(self):
        self.install_clock()
        base = self.tmpdir()
        trace = D.Diagnostics.start_run("check_interface", "carbon-1",
                                        base_dir=base)
        with trace.span("round[0]"):
            self.clock.tick(5)
            with trace.span("manager_turn"):
                self.clock.tick(20)
                with trace.span("provider_call"):
                    self.clock.tick(100)
                self.clock.tick(5)
            self.clock.tick(10)
        rollup = trace.close()

        by_path = {s.path: s for s in trace.spans}
        # provider_call ran exactly 100ms.
        self.assertEqual(by_path["round[0]>manager_turn>provider_call"].duration_ms, 100)
        # manager_turn wraps the 20 + 100 + 5 = 125ms.
        self.assertEqual(by_path["round[0]>manager_turn"].duration_ms, 125)
        # round[0] wraps 5 + 125 + 10 = 140ms.
        self.assertEqual(by_path["round[0]"].duration_ms, 140)
        # Paths reflect the nesting.
        self.assertEqual(
            by_path["round[0]>manager_turn>provider_call"].parent_id,
            by_path["round[0]>manager_turn"].span_id,
        )
        self.assertEqual(rollup["duration_ms"], 140)
        self.assertEqual(rollup["rounds"], 1)
        self.assertEqual(rollup["status"], "ok")


class TestBottleneckRanking(_ClockMixin, unittest.TestCase):
    """10.1.b -- correct ordering and pct relative to total run duration."""

    def test_ranking_and_pct(self):
        self.install_clock()
        base = self.tmpdir()
        trace = D.Diagnostics.start_run("check_crons", "carbon-2", base_dir=base)
        # Three sibling spans of known, distinct durations: 200, 50, 250 (ms).
        with trace.span("a"):
            self.clock.tick(200)
        with trace.span("b"):
            self.clock.tick(50)
        with trace.span("c"):
            self.clock.tick(250)
        rollup = trace.close()

        names = [b["name"] for b in rollup["bottlenecks"]]
        self.assertEqual(names[:3], ["c", "a", "b"])  # 250 > 200 > 50
        total = rollup["duration_ms"]  # 500
        self.assertEqual(total, 500)
        pct = {b["name"]: b["pct"] for b in rollup["bottlenecks"]}
        self.assertEqual(pct["c"], 50.0)
        self.assertEqual(pct["a"], 40.0)
        self.assertEqual(pct["b"], 10.0)

    def test_top_n_truncation(self):
        self.install_clock()
        base = self.tmpdir()
        trace = D.Diagnostics.start_run("check_crons", "carbon-2b", base_dir=base)
        for i in range(8):
            with trace.span(f"s{i}"):
                self.clock.tick(10 * (i + 1))
        rollup = trace.close()
        self.assertEqual(len(rollup["bottlenecks"]), D.DEFAULT_BOTTLENECK_TOP_N)
        # Highest-duration span (s7 = 80ms) ranks first.
        self.assertEqual(rollup["bottlenecks"][0]["name"], "s7")


class TestTokenSummation(_ClockMixin, unittest.TestCase):
    """10.1.c -- token summation across provider_call spans in a multi-round run."""

    def test_tokens_summed_across_rounds(self):
        self.install_clock()
        base = self.tmpdir()
        trace = D.Diagnostics.start_run("check_interface", "carbon-3",
                                        base_dir=base)
        for r in range(3):
            with trace.span(f"round[{r}]"):
                with trace.span("manager_turn"):
                    with trace.span("provider_call") as s:
                        s.set_tokens(input=100, output=50, cache_read=10,
                                     cache_creation=5, cost_usd=0.002,
                                     provider_duration_ms=1234)
        rollup = trace.close()

        tok = rollup["tokens"]
        self.assertEqual(tok["input"], 300)
        self.assertEqual(tok["output"], 150)
        self.assertEqual(tok["cache_read"], 30)
        self.assertEqual(tok["cache_creation"], 15)
        self.assertEqual(tok["total"], 300 + 150 + 30 + 15)
        self.assertEqual(rollup["provider_calls"], 3)
        self.assertEqual(rollup["rounds"], 3)
        self.assertAlmostEqual(rollup["cost_usd"], 0.006, places=6)

    def test_rollup_row_persisted_to_sqlite(self):
        self.install_clock()
        base = self.tmpdir()
        trace = D.Diagnostics.start_run("check_workers", "carbon-3b",
                                        base_dir=base, parent_run_id="parent-xyz")
        with trace.span("round[0]"):
            with trace.span("provider_call") as s:
                s.set_tokens(input=10, output=20, cost_usd=0.001)
        rollup = trace.close()

        rows = D.query_rollups(os.path.join(base, "rollups.sqlite"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["run_id"], rollup["run_id"])
        self.assertEqual(row["parent_run_id"], "parent-xyz")
        self.assertEqual(row["tokens_total"], 30)
        self.assertEqual(row["trigger"], "check_workers")


class TestFailOpen(_ClockMixin, unittest.TestCase):
    """10.1.d -- an exception inside a span must not prevent trace.close()."""

    def test_exception_in_span_still_closes(self):
        self.install_clock()
        base = self.tmpdir()
        trace = D.Diagnostics.start_run("check_interface", "carbon-4",
                                        base_dir=base)
        # The caller's exception MUST propagate out of the with-block...
        with self.assertRaises(ValueError):
            with trace.span("round[0]"):
                with trace.span("provider_call"):
                    raise ValueError("boom from agent code")
        # ...and the trace must still close and yield a valid rollup.
        rollup = trace.close()
        self.assertEqual(rollup["status"], "error")
        self.assertEqual(rollup["schema"], D.SCHEMA)
        self.assertIn("bottlenecks", rollup)
        provider = next(span for span in rollup["spans"] if span["name"] == "provider_call")
        self.assertEqual(provider["meta"]["error_type"], "ValueError")
        self.assertEqual(provider["meta"]["error_summary"], "boom from agent code")
        self.assertTrue(provider["meta"]["traceback"])

    def test_exception_summary_redacts_credentials(self):
        base = self.tmpdir()
        trace = D.Diagnostics.start_run("message", "carbon-secret", base_dir=base)
        with self.assertRaises(RuntimeError):
            with trace.span("tool"):
                raise RuntimeError("request failed for sct_live_abcdefghijk")
        rollup = trace.close()
        tool = next(span for span in rollup["spans"] if span["name"] == "tool")
        self.assertNotIn("sct_live", tool["meta"]["error_summary"])
        self.assertIn("redacted", tool["meta"]["error_summary"])

    def test_storage_failure_does_not_raise(self):
        # The suppressed exception is logged on purpose; quiet it for this test
        # so the expected, handled failure does not look like a real error.
        import logging
        D.log.setLevel(logging.CRITICAL)
        self.addCleanup(lambda: D.log.setLevel(logging.NOTSET))
        # Point the run at a path that cannot be created (a file as a dir).
        clash = tempfile.NamedTemporaryFile(delete=False)
        clash.write(b"x")
        clash.close()
        # base_dir is a child of a regular file -> makedirs fails -> in-memory.
        trace = D.Diagnostics.start_run("check_crons", "carbon-4b",
                                        base_dir=os.path.join(clash.name, "sub"))
        with trace.span("round[0]"):
            with trace.span("provider_call") as s:
                s.set_tokens(input=1, output=1)
        rollup = trace.close()  # must not raise
        self.assertTrue(trace._disabled)
        self.assertEqual(rollup["tokens"]["total"], 2)
        os.unlink(clash.name)


class TestContextVarIsolation(_ClockMixin, unittest.TestCase):
    """10.1.e -- concurrent runs on separate threads do not cross-contaminate."""

    def test_concurrent_threads_isolated(self):
        base = self.tmpdir()
        results = {}
        barrier = threading.Barrier(2)

        def worker(tag):
            trace = D.Diagnostics.start_run("check_interface", f"carbon-{tag}",
                                            base_dir=base)
            with trace.span(f"round[0]-{tag}"):
                barrier.wait()  # force the two threads to interleave here
                with trace.span(f"manager_turn-{tag}"):
                    with trace.span("provider_call") as s:
                        s.set_tokens(input=10 if tag == "A" else 99)
            results[tag] = trace.close()
            # Each trace must contain ONLY its own spans.
            results[tag + "_paths"] = sorted(s.path for s in trace.spans)

        tA = threading.Thread(target=worker, args=("A",))
        tB = threading.Thread(target=worker, args=("B",))
        tA.start(); tB.start(); tA.join(); tB.join()

        # No path from A leaked into B's span set and vice-versa.
        self.assertTrue(all("-A" in p or p == "provider_call"
                            for p in results["A_paths"]))
        self.assertTrue(all("-B" in p or p == "provider_call"
                            for p in results["B_paths"]))
        # Token capture stayed on the right run.
        self.assertEqual(results["A"]["tokens"]["input"], 10)
        self.assertEqual(results["B"]["tokens"]["input"], 99)
        # Distinct run ids.
        self.assertNotEqual(results["A"]["run_id"], results["B"]["run_id"])


class TestUnclosedRunTolerance(_ClockMixin, unittest.TestCase):
    """10.1.f -- partial JSONL from a mid-run re-exec is parseable."""

    def test_partial_jsonl_parses(self):
        self.install_clock()
        base = self.tmpdir()
        trace = D.Diagnostics.start_run("check_interface", "carbon-5",
                                        base_dir=base)
        trace.span("round[0]")            # opened, intentionally never closed
        inner = trace.span("manager_turn")  # opened, never closed
        path = trace._jsonl_path

        # Simulate a kill: append a half-written final line, do NOT close().
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"kind":"span_start","span_id":"torn"')  # no newline/brace

        parsed = D.read_trace(path)
        self.assertEqual(parsed["run_id"], trace.run_id)
        self.assertFalse(parsed["closed"])          # no run_close seen
        # The two opened spans are present and flagged open (no span_end).
        open_spans = [s for s in parsed["spans"].values()
                      if s.get("closed") is False]
        self.assertGreaterEqual(len(open_spans), 2)
        names = {s.get("name") for s in parsed["spans"].values()}
        self.assertIn("round[0]", names)
        self.assertIn("manager_turn", names)

    def test_closed_run_reads_back_rollup(self):
        self.install_clock()
        base = self.tmpdir()
        trace = D.Diagnostics.start_run("check_crons", "carbon-5b", base_dir=base)
        with trace.span("round[0]"):
            self.clock.tick(7)
        trace.close()
        parsed = D.read_trace(trace._jsonl_path)
        self.assertTrue(parsed["closed"])
        self.assertIsNotNone(parsed["rollup"])
        self.assertEqual(parsed["rollup"]["run_id"], trace.run_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
