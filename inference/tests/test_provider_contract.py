"""Every registered provider has to satisfy the same contract.

This is the test that fails when a new provider folder is added without
implementing the whole seam — which is exactly the failure that would otherwise
show up as an ``if provider == ...`` creeping back into worker or manager code.
"""
import unittest
from unittest import mock

import inference
from inference.base import InferenceProvider
from inference.config import BrainConfig
from inference.models import WorkerLaunchSpec


def _spec(**overrides):
    base = dict(
        worker_id="w-1",
        worker_type="terminal",
        task="do the thing",
        system_prompt="system prompt",
        session_id="session-1",
    )
    base.update(overrides)
    return WorkerLaunchSpec(**base)


class ProviderContractTest(unittest.TestCase):
    def providers(self):
        return [inference.get_provider(name) for name in inference.provider_names()]

    def test_every_provider_is_registered_under_its_own_name(self):
        for name in inference.provider_names():
            self.assertEqual(inference.get_provider(name).name, name)

    def test_every_provider_implements_the_whole_contract(self):
        for provider in self.providers():
            self.assertIsInstance(provider, InferenceProvider)
            for method in (
                "new_session",
                "run_turn",
                "worker_command",
                "read_output",
                "progress_events",
                "session_id_from_output",
                "has_completion_event",
                "terminal_state",
                "parse_output",
            ):
                self.assertTrue(
                    callable(getattr(provider, method, None)),
                    f"{provider.name} is missing {method}",
                )

    def test_every_provider_builds_a_launchable_worker_command(self, ):
        for provider in self.providers():
            with mock.patch("builtins.open", mock.mock_open()), \
                    mock.patch("os.makedirs"):
                command = provider.worker_command(_spec(scratch_dir="/tmp/scratch"))
            self.assertTrue(command.argv, provider.name)
            self.assertIn(
                command.stdin,
                {inference.STDIN_NONE, inference.STDIN_TASK, inference.STDIN_STREAM},
            )

    def test_every_provider_reads_empty_output_without_claiming_success(self):
        for provider in self.providers():
            outcome = provider.read_output("")
            self.assertFalse(outcome.completed, provider.name)
            self.assertEqual(outcome.state, "failed", provider.name)
            self.assertTrue(outcome.result, provider.name)

    def test_unknown_provider_names_are_refused(self):
        with self.assertRaises(ValueError):
            inference.get_provider("telepathy")


class BrainConfigTest(unittest.TestCase):
    def test_an_unknown_brain_falls_back_to_claude(self):
        self.assertEqual(BrainConfig.model_validate({"brain": "telepathy"}).brain,
                         "claude")

    def test_chatgpt_is_an_alias_for_codex(self):
        config = BrainConfig.model_validate({"brain_order": ["chatgpt", "claude"]})
        self.assertEqual(config.order(), ["codex", "claude"])

    def test_duplicates_and_junk_are_dropped_from_the_order(self):
        config = BrainConfig.model_validate(
            {"brain": "codex", "brain_order": ["claude", "claude", 7, "nope"]}
        )
        self.assertEqual(config.order(), ["claude"])

    def test_an_empty_order_falls_back_to_the_brain(self):
        config = BrainConfig.model_validate({"brain": "codex", "brain_order": []})
        self.assertEqual(config.order(), ["codex"])


if __name__ == "__main__":
    unittest.main()
