"""
tests/test_token_harvest.py

Phase 2 token-harvest tests (memo Section 10.2).

  10.2.a  Claude: recorded 'result' fixtures -> normalized usage matches expected
  10.2.b  Codex:  'turn/completed' fixture -> normalization parity (PROVISIONAL,
          pending a real captured event per Q1)
  +       Backward compatibility: DONE events without a usage block are byte-
          identical to pre-Phase-2 output; display lines unchanged
  +       Integration: harvested usage flows into the Phase 1 tracer rollup

Pure stdlib unittest, no external dependencies.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from interface import progress as P
from inference.claude import progress as CLAUDE_P
from inference.codex import progress as CODEX_P          # noqa: E402
from diagnostics import store as D       # noqa: E402


def _done(events):
    return next(e for e in events if e.get("kind") == P.DONE)


# --- representative fixtures -------------------------------------------------
# Claude stream-json terminal 'result' event. Field names per the memo and the
# standard Claude Code stream-json schema.
CLAUDE_RESULT_WITH_USAGE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 4210,
    "num_turns": 3,
    "result": "All done.",
    "total_cost_usd": 0.01734,
    "usage": {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 50,
    },
}

CLAUDE_RESULT_NO_USAGE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 980,
    "result": "ok",
    "total_cost_usd": 0.002,
}

# Codex tokens arrive on a thread/tokenUsage/updated notification (params.
# tokenUsage.last), NOT on turn/completed. Schema LOCKED against a real captured
# event (memo Q1, 2026-06-26). Note cachedInputTokens is a subset of inputTokens.
CODEX_TOKEN_USAGE_UPDATED = {
    "method": "thread/tokenUsage/updated",
    "params": {
        "threadId": "019f033a-264a-7b43-be01-5e5a6cb5e239",
        "turnId": "019f033a-26ec-7a62-8da6-37a3f5ddaeb5",
        "tokenUsage": {
            "total": {"totalTokens": 27255, "inputTokens": 27174,
                      "cachedInputTokens": 2432, "outputTokens": 81,
                      "reasoningOutputTokens": 74},
            "last": {"totalTokens": 27255, "inputTokens": 27174,
                     "cachedInputTokens": 2432, "outputTokens": 81,
                     "reasoningOutputTokens": 74},
            "modelContextWindow": 258400,
        },
    },
}

# A multi-turn thread where `last` (this turn) diverges from `total`
# (cumulative). Exercises the "use last, not total" rule that the single-turn
# capture above cannot, since there last == total.
CODEX_TOKEN_USAGE_MULTI_TURN = {
    "method": "thread/tokenUsage/updated",
    "params": {
        "tokenUsage": {
            "total": {"totalTokens": 99999, "inputTokens": 90000,
                      "cachedInputTokens": 50000, "outputTokens": 9999},
            "last": {"totalTokens": 5040, "inputTokens": 5000,
                     "cachedInputTokens": 1000, "outputTokens": 40},
            "modelContextWindow": 258400,
        },
    },
}

CODEX_TURN_COMPLETED = {
    "method": "turn/completed",
    "params": {"turn": {"status": "completed", "durationMs": 5300}},
}

CODEX_CONTEXT = {
    "type": "silicon.codex_context",
    "model": "gpt-5.6-sol",
    "model_provider": "openai",
}

CODEX_TURN_NO_USAGE = {
    "method": "turn/completed",
    "params": {"turn": {"status": "completed", "durationMs": 1500}},
}


class TestClaudeTokenHarvest(unittest.TestCase):
    """10.2.a"""

    def test_usage_normalized_from_result(self):
        done = _done(CLAUDE_P.claude_progress_events(CLAUDE_RESULT_WITH_USAGE))
        usage = done.get("usage")
        self.assertIsNotNone(usage)
        self.assertEqual(usage["input"], 1200)
        self.assertEqual(usage["output"], 340)
        self.assertEqual(usage["cache_read"], 900)
        self.assertEqual(usage["cache_creation"], 50)
        self.assertEqual(usage["num_turns"], 3)
        # Pre-existing fields are untouched.
        self.assertEqual(done["duration_ms"], 4210)
        self.assertAlmostEqual(done["cost_usd"], 0.01734)

    def test_unified_accessor_maps_to_set_tokens(self):
        done = _done(CLAUDE_P.claude_progress_events(CLAUDE_RESULT_WITH_USAGE))
        kw = P.usage_from_done_event(done)
        self.assertEqual(kw["input"], 1200)
        self.assertEqual(kw["output"], 340)
        self.assertEqual(kw["cache_read"], 900)
        self.assertEqual(kw["cache_creation"], 50)
        self.assertAlmostEqual(kw["cost_usd"], 0.01734)
        self.assertEqual(kw["provider_duration_ms"], 4210)
        self.assertEqual(kw["num_turns"], 3)

    def test_model_identity_propagates_from_claude_init(self):
        state = {}
        self.assertEqual(CLAUDE_P.claude_progress_events({
            "type": "system", "subtype": "init", "model": "claude-opus-4-6",
        }, state), [])
        done = _done(CLAUDE_P.claude_progress_events(CLAUDE_RESULT_WITH_USAGE, state))
        self.assertEqual(done["model"], "claude-opus-4-6")
        self.assertEqual(done["model_provider"], "anthropic")
        self.assertEqual(P.usage_from_done_event(done)["model"], "claude-opus-4-6")

    def test_error_summary_uses_is_error_and_redacts_credentials(self):
        event = {
            "kind": P.DONE,
            "status": "success",
            "is_error": True,
            "preview": "Failed to authenticate api_key=sk-examplecredential12345",
        }
        self.assertTrue(P.progress_is_error(event))
        summary = P.diagnostic_error_summary(event)
        self.assertIn("Failed to authenticate", summary)
        self.assertNotIn("sk-examplecredential12345", summary)


class TestBackwardCompatibility(unittest.TestCase):
    """Phase 2 must be strictly additive (memo 4.2)."""

    def test_result_without_usage_omits_key(self):
        done = _done(CLAUDE_P.claude_progress_events(CLAUDE_RESULT_NO_USAGE))
        self.assertNotIn("usage", done)              # key omitted, not null
        self.assertEqual(done["duration_ms"], 980)   # existing fields intact
        self.assertAlmostEqual(done["cost_usd"], 0.002)

    def test_accessor_safe_on_legacy_done(self):
        done = _done(CLAUDE_P.claude_progress_events(CLAUDE_RESULT_NO_USAGE))
        kw = P.usage_from_done_event(done)
        self.assertEqual(kw["input"], 0)
        self.assertEqual(kw["output"], 0)
        self.assertEqual(kw["provider_duration_ms"], 980)

    def test_display_line_unchanged_by_usage(self):
        # The new usage key must not alter the human-facing display line.
        with_usage = _done(CLAUDE_P.claude_progress_events(CLAUDE_RESULT_WITH_USAGE))
        line = P.progress_display_line(with_usage)
        self.assertEqual(line, "done 4.2s $0.0173")

    def test_non_result_events_unaffected(self):
        # A tool_use assistant event still parses exactly as before.
        evt = {
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/x.py"}},
            ]},
        }
        events = CLAUDE_P.claude_progress_events(evt)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], P.READING_FILE)
        self.assertEqual(events[0]["path"], "/x.py")
        self.assertNotIn("usage", events[0])


class TestCodexTokenHarvest(unittest.TestCase):
    """10.2.b -- LOCKED against the real captured schema (memo Q1)."""

    def test_token_usage_event_captures_last_without_emitting(self):
        # The tokenUsage notification stashes usage in state and is not itself a
        # display/DONE event.
        state = {}
        out = CODEX_P.codex_progress_event(CODEX_TOKEN_USAGE_UPDATED, state)
        self.assertIsNone(out)
        self.assertIn(CODEX_P._CODEX_LAST_USAGE_STATE_KEY, state)

    def test_usage_normalized_on_turn_completed(self):
        state = {}
        CODEX_P.codex_progress_event(CODEX_TOKEN_USAGE_UPDATED, state)
        done = CODEX_P.codex_progress_event(CODEX_TURN_COMPLETED, state)
        usage = done.get("usage")
        self.assertIsNotNone(usage)
        # cachedInputTokens (2432) is a SUBSET of inputTokens (27174): subtract.
        self.assertEqual(usage["input"], 27174 - 2432)   # 24742
        self.assertEqual(usage["cache_read"], 2432)
        self.assertEqual(usage["output"], 81)
        self.assertEqual(usage["cache_creation"], 0)
        # The four buckets reconstruct totalTokens exactly (the whole point).
        self.assertEqual(
            usage["input"] + usage["output"] + usage["cache_read"] + usage["cache_creation"],
            27255,
        )
        self.assertEqual(done["duration_ms"], 5300)

    def test_uses_last_not_total_on_multi_turn(self):
        state = {}
        CODEX_P.codex_progress_event(CODEX_TOKEN_USAGE_MULTI_TURN, state)
        done = CODEX_P.codex_progress_event(CODEX_TURN_COMPLETED, state)
        usage = done["usage"]
        # Must reflect this turn (last: 5000/1000/40), not the cumulative total.
        self.assertEqual(usage["input"], 5000 - 1000)   # 4000
        self.assertEqual(usage["cache_read"], 1000)
        self.assertEqual(usage["output"], 40)
        self.assertEqual(
            usage["input"] + usage["output"] + usage["cache_read"] + usage["cache_creation"],
            5040,
        )

    def test_no_usage_omits_key_and_does_not_crash(self):
        # turn/completed with no preceding tokenUsage event -> key omitted.
        done = CODEX_P.codex_progress_event(CODEX_TURN_NO_USAGE, {})
        self.assertNotIn("usage", done)
        self.assertEqual(done["status"], "completed")

    def test_accessor_maps_codex_usage_to_set_tokens(self):
        state = {}
        CODEX_P.codex_progress_event(CODEX_CONTEXT, state)
        CODEX_P.codex_progress_event(CODEX_TOKEN_USAGE_UPDATED, state)
        done = CODEX_P.codex_progress_event(CODEX_TURN_COMPLETED, state)
        kw = P.usage_from_done_event(done)
        self.assertEqual(kw["input"], 24742)
        self.assertEqual(kw["cache_read"], 2432)
        self.assertEqual(kw["output"], 81)
        self.assertEqual(kw["cache_creation"], 0)
        self.assertEqual(kw["provider_duration_ms"], 5300)
        self.assertEqual(kw["cost_usd"], 0.0)  # Codex exposes no cost
        self.assertEqual(kw["model"], "gpt-5.6-sol")
        self.assertEqual(kw["model_provider"], "openai")


class TestTracerIntegration(unittest.TestCase):
    """Harvested usage flows into the Phase 1 rollup via set_tokens."""

    def test_harvest_feeds_rollup(self):
        import tempfile
        base = tempfile.mkdtemp(prefix="diag_harvest_")
        trace = D.Diagnostics.start_run("check_interface", "carbon-x",
                                        base_dir=base)
        with trace.span("round[0]"):
            with trace.span("manager_turn"):
                with trace.span("provider_call") as s:
                    done = _done(CLAUDE_P.claude_progress_events(CLAUDE_RESULT_WITH_USAGE))
                    s.set_tokens(**P.usage_from_done_event(done))
        rollup = trace.close()
        self.assertEqual(rollup["tokens"]["input"], 1200)
        self.assertEqual(rollup["tokens"]["output"], 340)
        self.assertEqual(rollup["tokens"]["cache_read"], 900)
        self.assertEqual(rollup["tokens"]["cache_creation"], 50)
        self.assertEqual(rollup["tokens"]["total"], 1200 + 340 + 900 + 50)
        self.assertEqual(rollup["provider_calls"], 1)
        self.assertAlmostEqual(rollup["cost_usd"], 0.01734, places=5)

    def test_codex_harvest_feeds_rollup(self):
        # The exact path manager.py's codex provider_call span will follow:
        # tokenUsage/updated stashes usage -> turn/completed emits DONE ->
        # usage_from_done_event -> span.set_tokens -> rollup.
        import tempfile
        base = tempfile.mkdtemp(prefix="diag_codex_harvest_")
        state = {}
        CODEX_P.codex_progress_event(CODEX_TOKEN_USAGE_UPDATED, state)
        trace = D.Diagnostics.start_run("manager_loop", "carbon-codex",
                                        base_dir=base)
        with trace.span("round[0]"):
            with trace.span("manager_turn"):
                with trace.span("provider_call") as s:
                    done = CODEX_P.codex_progress_event(CODEX_TURN_COMPLETED, state)
                    s.set_tokens(**P.usage_from_done_event(done))
        rollup = trace.close()
        self.assertEqual(rollup["tokens"]["input"], 24742)
        self.assertEqual(rollup["tokens"]["cache_read"], 2432)
        self.assertEqual(rollup["tokens"]["output"], 81)
        self.assertEqual(rollup["tokens"]["cache_creation"], 0)
        # Rollup total reconstructs Codex totalTokens.
        self.assertEqual(rollup["tokens"]["total"], 27255)
        self.assertEqual(rollup["provider_calls"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
