import sys
import types
import unittest
from unittest import mock

from prompts import DNA


class QuestionnairePromptTest(unittest.TestCase):
    def test_questionnaire_is_loaded_once_into_every_manager_prompt(self):
        fake_extend = types.ModuleType("core.extend")
        fake_extend.render_manager_catalog = lambda: ""

        with (
            mock.patch.object(DNA, "_get_contact_info", return_value=None),
            mock.patch.object(DNA, "_glass_profile_section", return_value=""),
            mock.patch.object(DNA, "_glass_team_context_section", return_value=""),
            mock.patch.dict(sys.modules, {"core.extend": fake_extend}),
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

    def test_questionnaire_requires_scope_based_tasks_and_a_pre_send_truth_gate(self):
        questionnaire = DNA._read_prompt("QUESTIONNAIRE.md")
        prose = " ".join(questionnaire.split())

        self.assertIn("Never create a task or Todo merely because time elapsed", prose)
        self.assertIn(
            "runtime automatically schedules internal accuracy checkpoints "
            "at every 5% of the accepted goal time",
            prose,
        )
        self.assertIn("until the task reaches a terminal state", prose)
        self.assertIn(
            "rescheduled when the accepted estimate materially changes",
            prose,
        )
        self.assertIn("Immediately before ANY Carbon-facing message", prose)
        self.assertIn("Publish any real missing state transition", prose)
        self.assertIn("only then send the message", prose)

    def test_questionnaire_requires_reasoned_answers_without_answer_menus(self):
        questionnaire = DNA._read_prompt("QUESTIONNAIRE.md")
        prose = " ".join(questionnaire.split())
        binary_question = (
            r"(?m)^\d+\)\s+"
            r"(?:Is|Are|Do|Does|Did|Can|Could|Should|Will|Would|Has|Have|Had)\b"
        )

        self.assertNotRegex(questionnaire, binary_question)
        self.assertNotRegex(questionnaire, r"\bIf yes\b")
        self.assertNotRegex(questionnaire, r"\bIf no\b")
        self.assertNotIn("until the answer is yes", questionnaire)
        self.assertIn(
            "write a concise, reasoned answer in the internal work",
            prose,
        )
        self.assertIn(
            "Do not answer with yes/no, a category label, or a selection from "
            "supplied alternatives",
            prose,
        )
        self.assertNotIn("Classify it as", questionnaire)
        self.assertNotIn("When no external tool is required", questionnaire)
        self.assertNotIn(
            "whether they need a direct answer, a focused question",
            prose,
        )
        self.assertIn(
            "what evidence proves that the visible work state and every claim",
            prose,
        )


if __name__ == "__main__":
    unittest.main()
