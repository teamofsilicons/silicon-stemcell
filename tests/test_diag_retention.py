"""
tests/test_diag_retention.py -- unit coverage for core/diag_retention.py.

Written against this project's documented failure pattern: fail-open code that
runs green while silently doing nothing, and concurrency bugs that pass a
single run. Hence: an explicit deletes-the-right-files-and-ONLY-those test, a
throttle test with injected clocks (no sleeping), a fail-open test that also
asserts the sweep still deleted what it could (not just "didn't raise"), and a
looped multi-threaded close-trigger test (10 iterations, matching the
loop-verify rule from the Phase 3 WAL fix).
"""

import os
import shutil
import tempfile
import threading
import time
import unittest

from core.diag_retention import (
    DIAG_RETENTION_DAYS,
    maybe_prune,
    prune_diagnostic_traces,
)

DAY = 86_400.0


def _write(dirpath, name, age_days, now, content=b"x"):
    path = os.path.join(dirpath, name)
    with open(path, "wb") as fh:
        fh.write(content)
    t = now - age_days * DAY
    os.utime(path, (t, t))
    return path


class RetentionTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="diag_retention_test_")
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def names(self):
        return sorted(os.listdir(self.dir))


class TestPruneCutoff(RetentionTestBase):
    def test_default_retention_is_indefinite(self):
        self.assertIsNone(DIAG_RETENTION_DAYS)

    def test_default_policy_deletes_nothing(self):
        _write(self.dir, "old.jsonl", 91, self.now)
        _write(self.dir, "edge_keep.jsonl", 89.9, self.now)
        _write(self.dir, "fresh.jsonl", 0, self.now)
        s = prune_diagnostic_traces(base_dir=self.dir, now=self.now)
        self.assertEqual(s["deleted"], 0)
        self.assertEqual(s["scanned"], 0)
        self.assertEqual(s["errors"], 0)
        self.assertEqual(
            self.names(),
            ["edge_keep.jsonl", "fresh.jsonl", "old.jsonl"],
        )

    def test_only_jsonl_is_touched(self):
        _write(self.dir, "old.jsonl", 400, self.now)
        _write(self.dir, "rollups.sqlite", 400, self.now)
        _write(self.dir, "rollups.sqlite-wal", 400, self.now)
        _write(self.dir, "rollups.sqlite-shm", 400, self.now)
        _write(self.dir, ".last_prune", 400, self.now)
        _write(self.dir, "notes.txt", 400, self.now)
        os.mkdir(os.path.join(self.dir, "subdir.jsonl"))
        s = prune_diagnostic_traces(
            base_dir=self.dir, retention_days=90, now=self.now
        )
        self.assertEqual(s["deleted"], 1)
        self.assertIn("rollups.sqlite", self.names())
        self.assertIn(".last_prune", self.names())
        self.assertIn("notes.txt", self.names())
        self.assertIn("subdir.jsonl", self.names())
        self.assertNotIn("old.jsonl", self.names())

    def test_dry_run_reports_but_deletes_nothing(self):
        _write(self.dir, "old.jsonl", 120, self.now, content=b"abcde")
        s = prune_diagnostic_traces(
            base_dir=self.dir, retention_days=90, dry_run=True, now=self.now
        )
        self.assertEqual(s["deleted"], 1)
        self.assertEqual(s["bytes_freed"], 5)
        self.assertTrue(s["dry_run"])
        self.assertIn("old.jsonl", self.names())

    def test_custom_retention_days_respected(self):
        _write(self.dir, "eight_days.jsonl", 8, self.now)
        s = prune_diagnostic_traces(
            base_dir=self.dir, retention_days=7, now=self.now
        )
        self.assertEqual(s["deleted"], 1)

    def test_missing_directory_is_a_noop(self):
        s = prune_diagnostic_traces(
            base_dir=os.path.join(self.dir, "does_not_exist"),
            retention_days=90,
            now=self.now,
        )
        self.assertEqual(s["scanned"], 0)
        self.assertEqual(s["deleted"], 0)


class TestFailOpen(RetentionTestBase):
    def test_undeletable_file_does_not_abort_sweep(self):
        _write(self.dir, "a_old.jsonl", 100, self.now)
        _write(self.dir, "b_old.jsonl", 100, self.now)

        real_remove = os.remove
        calls = {"n": 0}

        def flaky_remove(path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated permission denied")
            return real_remove(path)

        os.remove = flaky_remove
        try:
            s = prune_diagnostic_traces(
                base_dir=self.dir, retention_days=90, now=self.now
            )
        finally:
            os.remove = real_remove

        self.assertEqual(s["errors"], 1)
        self.assertEqual(s["deleted"], 1)
        remaining = [n for n in self.names() if n.endswith(".jsonl")]
        self.assertEqual(len(remaining), 1)

    def test_maybe_prune_never_raises_even_if_sweep_explodes(self):
        _write(self.dir, "old.jsonl", 100, self.now)
        import core.diag_retention as R
        real = R.prune_diagnostic_traces
        R.prune_diagnostic_traces = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        try:
            result = maybe_prune(
                base_dir=self.dir, retention_days=90, now=self.now
            )
        finally:
            R.prune_diagnostic_traces = real
        self.assertIsNone(result)


class TestThrottle(RetentionTestBase):
    def test_second_close_inside_window_skips(self):
        _write(self.dir, "old.jsonl", 100, self.now)
        first = maybe_prune(
            base_dir=self.dir, retention_days=90, now=self.now
        )
        self.assertIsNotNone(first)
        self.assertEqual(first["deleted"], 1)
        _write(self.dir, "old2.jsonl", 100, self.now)
        second = maybe_prune(
            base_dir=self.dir, retention_days=90, now=self.now + 60
        )
        self.assertIsNone(second)
        self.assertIn("old2.jsonl", self.names())

    def test_close_after_window_prunes_again(self):
        maybe_prune(base_dir=self.dir, retention_days=90, now=self.now)
        _write(self.dir, "old2.jsonl", 100, self.now)
        later = self.now + 6 * 3600 + 1
        third = maybe_prune(
            base_dir=self.dir, retention_days=90, now=later
        )
        self.assertIsNotNone(third)
        self.assertEqual(third["deleted"], 1)
        self.assertNotIn("old2.jsonl", self.names())

    def test_marker_touched_before_sweep_prevents_retry_storm(self):
        import core.diag_retention as R
        real = R.prune_diagnostic_traces
        R.prune_diagnostic_traces = lambda **kw: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        try:
            maybe_prune(
                base_dir=self.dir, retention_days=90, now=self.now
            )
        finally:
            R.prune_diagnostic_traces = real
        nxt = maybe_prune(
            base_dir=self.dir, retention_days=90, now=self.now + 60
        )
        self.assertIsNone(nxt)


class TestConcurrentCloses(RetentionTestBase):
    ITERATIONS = 10
    THREADS = 8

    def test_concurrent_closes_are_safe_and_correct(self):
        for i in range(self.ITERATIONS):
            sub = os.path.join(self.dir, f"iter{i}")
            os.mkdir(sub)
            now = time.time()
            for k in range(5):
                _write(sub, f"old{k}.jsonl", 100 + k, now)
            for k in range(3):
                _write(sub, f"keep{k}.jsonl", 1, now)

            errors = []
            barrier = threading.Barrier(self.THREADS)

            def closer():
                try:
                    barrier.wait()
                    maybe_prune(
                        base_dir=sub, retention_days=90, now=now
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=closer) for _ in range(self.THREADS)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"iteration {i}: exceptions escaped")
            names = sorted(os.listdir(sub))
            self.assertEqual(
                [n for n in names if n.startswith("old")],
                [],
                f"iteration {i}: old traces survived",
            )
            self.assertEqual(
                [n for n in names if n.startswith("keep")],
                ["keep0.jsonl", "keep1.jsonl", "keep2.jsonl"],
                f"iteration {i}: recent traces were wrongly deleted",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
