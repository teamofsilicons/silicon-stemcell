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
        questionnaire = DNA._read_prompt("QUESTIONNAIRE.md").strip()
        self.assertTrue(prompt.rstrip().endswith(questionnaire))
        silicon_prompt = DNA._read_prompt("SILICON.md")
        self.assertNotIn("QUESTIONNAIRE.md", silicon_prompt)


if __name__ == "__main__":
    unittest.main()
