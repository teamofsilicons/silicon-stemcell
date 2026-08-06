import re
import unittest
from unittest import mock

from prompts import DNA


class QuestionnairePromptTest(unittest.TestCase):
    def test_questionnaire_is_loaded_once_into_every_manager_prompt(self):
        with (
            mock.patch.object(DNA, "_get_contact_info", return_value=None),
            mock.patch.object(DNA, "_glass_profile_section", return_value=""),
            mock.patch.object(DNA, "_glass_team_context_section", return_value=""),
        ):
            prompt = DNA.get_manager_prompt("carbon-1")

        self.assertEqual(prompt.count("prompts/QUESTIONNAIRE.md"), 1)
        self.assertEqual(prompt.count("prompts/FINAL_QUESTIONNAIRE.md"), 1)
        questionnaire = DNA._read_prompt("QUESTIONNAIRE.md").strip()
        final_questionnaire = DNA._read_prompt("FINAL_QUESTIONNAIRE.md").strip()
        self.assertLess(prompt.index(questionnaire), prompt.index(final_questionnaire))
        self.assertTrue(prompt.rstrip().endswith(final_questionnaire))
        silicon_prompt = DNA._read_prompt("SILICON.md")
        self.assertNotIn("QUESTIONNAIRE.md", silicon_prompt)
        self.assertNotIn("FINAL_QUESTIONNAIRE.md", silicon_prompt)

    def test_questionnaire_keeps_its_internal_only_contract(self):
        """The questionnaire is an internal decision process, never user-facing.

        The prose changed with the v2.2.x prompt rework; what must not change is
        that the reasoning stays out of the reply and that work is delegated
        rather than performed inline by the manager.
        """
        questionnaire = DNA._read_prompt("QUESTIONNAIRE.md")
        prose = " ".join(questionnaire.split())

        self.assertIn("NEVER APPEAR IN THE RESPONSE ITSELF", prose)
        self.assertIn("DO NOT MENTION THE QUESTIONNAIRE", prose)
        self.assertIn("internal decision process", prose)
        self.assertIn(
            "Never perform time taking actions as a manager yourself", prose
        )

    def test_questionnaire_is_a_numbered_list_of_questions(self):
        questionnaire = DNA._read_prompt("QUESTIONNAIRE.md")
        numbered = re.findall(r"(?m)^(\d+)\)\s+\S", questionnaire)
        self.assertGreaterEqual(len(numbered), 10)
        # Sequential from 1, so no question is dropped or duplicated in edits.
        self.assertEqual(numbered, [str(i) for i in range(1, len(numbered) + 1)])
