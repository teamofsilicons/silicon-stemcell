"""Regression tests derived from retained diagnostics failure evidence."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import manager  # noqa: E402
from core import diagnostics  # noqa: E402
from core.diagnostics import Diagnostics  # noqa: E402


class ManagerFailureDiagnosticsTests(unittest.TestCase):
    def test_expired_oauth_text_is_a_provider_failure(self):
        self.assertTrue(manager._manager_provider_failed(
            "Failed to authenticate: OAuth session expired and could not be refreshed",
            None,
        ))

    def test_structured_manager_error_still_triggers_provider_fallback(self):
        output = manager._safe_manager_error_tools(
            RuntimeError("provider process exited")
        )

        self.assertTrue(manager._manager_provider_failed(output, None))

    def test_provider_is_error_marks_span_and_run_error(self):
        trace = Diagnostics.start_run(
            "message", "carbon-x", base_dir=tempfile.mkdtemp(prefix="diag-provider-error-")
        )
        with trace.span("provider_call") as span:
            manager._attach_usage_to_span(span, {
                "kind": "done",
                "status": "success",
                "is_error": True,
                "preview": "Failed to authenticate",
            })
        rollup = trace.close()
        self.assertEqual(span.status, "error")
        self.assertEqual(span.meta["error"], "Failed to authenticate")
        self.assertEqual(rollup["status"], "error")
        self.assertEqual(rollup["meta"]["runtime"]["diagnostics_schema"], "silicon.diag/v2")
        self.assertIn("python", rollup["meta"]["runtime"])
        self.assertEqual(len(rollup["meta"]["runtime"]["config_sha256"]), 64)
        self.assertEqual(len(rollup["meta"]["runtime"]["prompt_manifest_sha256"]), 64)

    def test_trace_limits_report_evidence_loss_instead_of_rejecting_whole_run(self):
        old_events = diagnostics.MAX_TRACE_EVENTS
        old_spans = diagnostics.MAX_TRACE_SPANS
        diagnostics.MAX_TRACE_EVENTS = 1
        diagnostics.MAX_TRACE_SPANS = 1
        self.addCleanup(setattr, diagnostics, "MAX_TRACE_EVENTS", old_events)
        self.addCleanup(setattr, diagnostics, "MAX_TRACE_SPANS", old_spans)

        trace = Diagnostics.start_run(
            "message", "carbon", base_dir=tempfile.mkdtemp(prefix="diag-limits-")
        )
        trace.event("kept")
        trace.event("dropped")
        with trace.span("kept"):
            pass
        with trace.span("dropped"):
            pass
        rollup = trace.close()

        self.assertEqual(len(rollup["events"]), 1)
        self.assertEqual(len(rollup["spans"]), 1)
        self.assertEqual(rollup["meta"]["diagnostics_evidence"]["dropped_events"], 1)
        self.assertEqual(rollup["meta"]["diagnostics_evidence"]["dropped_spans"], 1)

    def test_graceful_shutdown_persists_active_run_as_failed(self):
        base = tempfile.mkdtemp(prefix="diag-shutdown-")
        trace = Diagnostics.start_run("message", "shutdown-carbon", base_dir=base)
        Diagnostics.register_active("shutdown-carbon", trace)

        self.assertGreaterEqual(Diagnostics.close_active_runs(
            reason="Silicon process received SIGTERM",
            category="process_signal",
        ), 1)

        rows = diagnostics.query_rollups(os.path.join(base, "rollups.sqlite"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(trace.meta["terminal_failure"]["category"], "process_signal")


if __name__ == "__main__":
    unittest.main()
