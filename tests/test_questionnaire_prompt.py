"""The setup questions a manager answers before it does anything.

The old QUESTIONNAIRE.md / FINAL_QUESTIONNAIRE.md pair was replaced by
prompts/SETUP_QUESTIONS.py, which is a tree rather than prose so it can be
walked in rounds. These tests hold the properties that make it usable: it is
rendered into every manager prompt, it comes last, every file it can pull in
exists, and it forbids acting during the thinking phase.
"""
import unittest
from unittest import mock

from prompts import DNA
from prompts import SETUP_QUESTIONS


class SetupQuestionsPromptTest(unittest.TestCase):
    def _manager_prompt(self):
        with (
            mock.patch.object(DNA, "_get_contact_info", return_value=None),
            mock.patch.object(DNA, "_glass_profile_section", return_value=""),
            mock.patch.object(DNA, "_glass_team_context_section", return_value=""),
        ):
            return DNA.get_manager_prompt("carbon-1")

    def test_setup_questions_are_rendered_once_and_come_last(self):
        prompt = self._manager_prompt()
        rendered = SETUP_QUESTIONS.render().rstrip()

        # Other prompts mention the file by name, so count the rendered tree
        # itself rather than the path.
        self.assertEqual(prompt.count(SETUP_QUESTIONS.HEADER), 1)
        self.assertIn(rendered, prompt)
        self.assertTrue(prompt.rstrip().endswith(rendered))

    def test_superseded_questionnaires_are_no_longer_loaded(self):
        prompt = self._manager_prompt()

        self.assertNotIn("prompts/QUESTIONNAIRE.md", prompt)
        self.assertNotIn("prompts/FINAL_QUESTIONNAIRE.md", prompt)

    def test_thinking_phase_forbids_running_commands(self):
        rendered = SETUP_QUESTIONS.render()

        self.assertIn("Do not run any command during this phase", rendered)
        self.assertIn("iwantto", rendered)

    def test_every_question_is_reachable_and_numbered_in_order(self):
        rendered = SETUP_QUESTIONS.render()
        numbers = [
            line.split(".", 1)[0]
            for line in rendered.splitlines()
            if line[:1].isdigit() and ". " in line
        ]

        self.assertGreaterEqual(len(SETUP_QUESTIONS.QUESTIONS), 10)
        self.assertEqual(
            numbers,
            [str(index) for index in range(1, len(SETUP_QUESTIONS.QUESTIONS) + 1)],
        )
        for entry in SETUP_QUESTIONS.QUESTIONS:
            self.assertEqual(len(entry), 1, "each entry holds exactly one question")
            for question in entry:
                self.assertIn(question, rendered)

    def test_every_included_file_exists(self):
        for filename in SETUP_QUESTIONS.included_files():
            with self.subTest(filename=filename):
                self.assertTrue(
                    DNA._prompt_path(filename.removeprefix("prompts/")),
                )
                self.assertNotEqual(DNA._read_prompt(filename.removeprefix("prompts/")), "")


if __name__ == "__main__":
    unittest.main()
