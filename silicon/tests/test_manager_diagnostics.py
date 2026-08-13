"""Regression tests derived from retained diagnostics failure evidence."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import inference  # noqa: E402
from diagnostics import store as diagnostics  # noqa: E402
from diagnostics.store import Diagnostics  # noqa: E402
from interface.progress import (  # noqa: E402
    provider_authentication_failed,
    provider_not_authenticated_message,
)
from inference import telemetry  # noqa: E402
from inference.claude import provider as claude_provider  # noqa: E402
from inference.claude.stream import StreamResult  # noqa: E402
from inference.codex import provider as codex_provider  # noqa: E402


class ManagerFailureDiagnosticsTests(unittest.TestCase):
    def test_provider_authentication_messages_name_the_provider(self):
        self.assertTrue(provider_authentication_failed(
            "authentication_failed: Not logged in"
        ))
        self.assertEqual(
            provider_not_authenticated_message("claude"),
            "Claude not authenticated.",
        )
        self.assertEqual(
            provider_not_authenticated_message("codex"),
            "Codex not authenticated.",
        )

    def test_claude_session_recovery_reports_authentication_failure(self):
        missing = StreamResult(
            returncode=1,
            stderr="[provider stderr omitted]",
            error_subtype="error_during_execution",
            error_message="No conversation found with session ID old-session",
        )
        authentication_failed = StreamResult(
            returncode=1,
            stderr="[provider authentication failed]",
            error_subtype="authentication_failed",
            error_message="authentication failed",
        )
        provider = claude_provider.ClaudeProvider()
        with (
            mock.patch.object(provider, "session_id", return_value="old-session"),
            mock.patch.object(provider, "new_session", return_value="new-session"),
            mock.patch.object(
                claude_provider, "prompt_file", return_value="/tmp/prompt"
            ),
            mock.patch.object(
                provider, "_stream", side_effect=[missing, authentication_failed]
            ),
        ):
            output, _rate_limit, _tools = provider.run_turn(
                inference.TurnRequest(
                    text="hello", contact_id="carbon-a", system_prompt="sp"
                )
            ).as_tuple()

        parsed = inference.parse_manager_output(output)
        self.assertEqual(
            parsed["tools"][0]["message"],
            "Claude not authenticated.",
        )
        self.assertTrue(inference.provider_failed(output, None))

    def test_codex_authentication_failure_names_codex(self):
        with mock.patch.object(
            codex_provider,
            "TracedAppServer",
            side_effect=RuntimeError("Not logged in"),
        ):
            output, _rate_limit, _tools = codex_provider.CodexProvider().run_turn(
                inference.TurnRequest(
                    text="hello", contact_id="carbon-a", system_prompt="sp"
                )
            ).as_tuple()

        parsed = inference.parse_manager_output(output)
        self.assertEqual(
            parsed["tools"][0]["message"],
            "Codex not authenticated.",
        )
        self.assertTrue(inference.provider_failed(output, None))

    def test_expired_oauth_text_is_a_provider_failure(self):
        self.assertTrue(inference.provider_failed(
            "Failed to authenticate: OAuth session expired and could not be refreshed",
            None,
        ))

    def test_structured_manager_error_still_triggers_provider_fallback(self):
        output = inference.error_tools(
            RuntimeError("provider process exited")
        )

        self.assertTrue(inference.provider_failed(output, None))

    def test_provider_is_error_marks_span_and_run_error(self):
        with tempfile.TemporaryDirectory(prefix="diag-provider-config-") as data_root:
            with open(
                os.path.join(data_root, "silicon.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("{}\n")
            with (
                mock.patch.object(diagnostics, "DATA_ROOT", data_root),
                mock.patch.object(
                    diagnostics,
                    "_RUNTIME_METADATA_CACHE",
                    None,
                ),
            ):
                trace = Diagnostics.start_run(
                    "message",
                    "carbon-x",
                    base_dir=tempfile.mkdtemp(prefix="diag-provider-error-"),
                )
                with trace.span("provider_call") as span:
                    telemetry.attach_usage(span, {
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
