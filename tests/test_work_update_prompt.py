"""How big work is described to a manager.

Work updates used to be a tool-JSON contract documented in WORK_UPDATES.md.
They are now `iwantto work`, so what these tests hold is the new contract: the
CLI reference is the single description of it, the manager prompt carries that
reference exactly once, and the three-level model the reference promises is
actually implemented.
"""
import argparse
import re
import unittest
from pathlib import Path
from unittest import mock

from prompts import DNA


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_REFERENCE_PATH = PROJECT_ROOT / "prompts" / "IWANTTO_CLI_REFERENCE.md"


def _manager_prompt():
    with (
        mock.patch.object(DNA, "_get_contact_info", return_value=None),
        mock.patch.object(DNA, "_glass_profile_section", return_value=""),
        mock.patch.object(DNA, "_glass_team_context_section", return_value=""),
    ):
        return DNA.get_manager_prompt("carbon-1")


class WorkPromptTest(unittest.TestCase):
    def test_manager_prompt_describes_work_exactly_once(self):
        prompt = _manager_prompt()

        self.assertEqual(prompt.count("prompts/IWANTTO_CLI_REFERENCE.md"), 1)
        self.assertIn("iwantto work", prompt)
        # The superseded tool-JSON guide must not come back alongside it.
        self.assertNotIn("prompts/WORK_UPDATES.md", prompt)
        self.assertFalse((PROJECT_ROOT / "prompts" / "WORK_UPDATES.md").exists())
        self.assertFalse(hasattr(DNA, "get_update_prompt"))

    def test_writer_resolves_one_shared_writing_guide(self):
        writer_prompt, error = DNA.get_worker_prompt("writer")
        guide = (PROJECT_ROOT / "prompts" / "shared" / "WRITING_STYLE.md").read_text(
            encoding="utf-8"
        )
        # A line distinctive enough to count occurrences of the whole guide.
        marker = "In short: signals, not proof."

        self.assertEqual(error, "")
        self.assertIn(marker, guide)
        self.assertEqual(writer_prompt.count(marker), 1)
        # The reference must be expanded, not passed through as literal text.
        self.assertNotIn(
            "{load-ref!prompts/shared/WRITING_STYLE.md}",
            writer_prompt,
        )

    def test_reference_documents_the_three_level_work_model(self):
        text = CLI_REFERENCE_PATH.read_text(encoding="utf-8")

        for flag in (
            "--new",
            "--add-task",
            "--add-subtask",
            "--list-subtask",
            "--expand",
            "--dispatch-update",
            "--blocker",
            "--completed",
            "--active",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, text)
        # Nothing is ever deleted; unfinished work is ended with a reason.
        self.assertIn("there is no way to delete a task", text)

    def test_every_documented_work_flag_is_accepted_by_the_cli(self):
        from core.iwantto.cli import build_parser

        section = re.search(
            r"### iwantto work\n(.*?)(?=\n## )",
            CLI_REFERENCE_PATH.read_text(encoding="utf-8"),
            flags=re.S,
        )
        self.assertIsNotNone(section, "the reference has no `iwantto work` section")
        documented = set(re.findall(r"--([a-z][a-z-]*)", section.group(1)))

        work_parser = next(
            action
            for action in build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["work"]
        accepted = {
            option.lstrip("-")
            for action in work_parser._actions
            for option in action.option_strings
        }

        self.assertEqual(sorted(documented - accepted), [])


if __name__ == "__main__":
    unittest.main()
