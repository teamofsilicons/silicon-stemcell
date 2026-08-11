"""Every model call leaves a record of what went in and what came out.

This is the trail a step is reconstructed from, so it has to survive a provider
that fails, a provider that times out, and a log directory that cannot be
written.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import inference
from diagnostics import logs
from inference.errors import ProviderTimeoutError
from inference.facade import Inference
from inference.models import TurnRequest, TurnResult


class InferenceJournalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = mock.patch.object(logs, "LOGS_DIR", self.root)
        self.patch.start()
        logs._instances.clear()
        self.request = TurnRequest(
            text="do the thing",
            contact_id="carbon-a",
            system_prompt="you are a manager",
        )

    def tearDown(self):
        self.patch.stop()
        logs._instances.clear()
        self.temp.cleanup()

    def records(self, kind="manager", agent_id="carbon-a"):
        path = self.root / "inference" / f"{kind}-{agent_id}.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").strip().splitlines()
        ]

    def test_a_turn_records_the_prompt_in_and_the_answer_out(self):
        engine = Inference(order=["claude"])
        with mock.patch.object(
            inference.get_provider("claude"),
            "run_turn",
            return_value=TurnResult('{"tools": [{"tool": "do_nothing"}]}', None, []),
        ):
            engine.run_turn(self.request)

        sent, received = self.records()
        self.assertEqual(sent["direction"], "in")
        self.assertEqual(sent["input"], "do the thing")
        self.assertEqual(sent["system_prompt"], "you are a manager")
        self.assertEqual(sent["provider"], "claude")
        self.assertEqual(received["direction"], "out")
        self.assertEqual(received["output"], '{"tools": [{"tool": "do_nothing"}]}')
        self.assertIn("seconds", received)

    def test_an_advisor_turn_lands_in_the_advisors_own_trail(self):
        engine = Inference(order=["claude"], kind="advisor")
        request = TurnRequest(
            text="should I?",
            contact_id="carbon-a",
            system_prompt="you advise",
            session_key="advisor__carbon-a",
        )
        with mock.patch.object(
            inference.get_provider("claude"),
            "run_turn",
            return_value=TurnResult("delegate it", None, []),
        ):
            engine.run_agent(request)

        self.assertEqual(self.records("manager", "carbon-a"), [])
        records = self.records("advisor", "advisor__carbon-a")
        self.assertEqual([r["direction"] for r in records], ["in", "out"])
        self.assertEqual(records[1]["output"], "delegate it")

    def test_a_failing_provider_is_recorded_before_the_fallback_runs(self):
        engine = Inference(order=["claude", "codex"])
        with (
            mock.patch.object(
                inference.get_provider("claude"),
                "run_turn",
                return_value=TurnResult("", None, []),
            ),
            mock.patch.object(
                inference.get_provider("codex"),
                "run_turn",
                return_value=TurnResult('{"tools": [{"tool": "reply", "message": "hi"}]}'),
            ),
        ):
            engine.run_turn(self.request)

        providers = [r["provider"] for r in self.records()]
        self.assertEqual(providers, ["claude", "claude", "codex", "codex"])

    def test_a_timeout_is_journaled_as_the_answer_it_became(self):
        """A turn that timed out is the one you most want in the trail."""
        engine = Inference(order=["claude"])
        with mock.patch.object(
            inference.get_provider("claude"),
            "run_turn",
            side_effect=ProviderTimeoutError("stopped responding"),
        ):
            result = engine.run_turn(self.request)

        self.assertEqual(result.output, inference.TIMEOUT_MSG)
        sent, received = self.records()
        self.assertEqual(sent["input"], "do the thing")
        self.assertEqual(received["output"], inference.TIMEOUT_MSG)

    def test_an_unwritable_log_never_fails_the_turn(self):
        engine = Inference(order=["claude"])
        with (
            mock.patch.object(
                inference.get_provider("claude"),
                "run_turn",
                return_value=TurnResult("answer", None, []),
            ),
            mock.patch("builtins.open", side_effect=OSError("read-only filesystem")),
        ):
            result = engine.run_turn(self.request)

        self.assertEqual(result.output, "answer")


if __name__ == "__main__":
    unittest.main()
