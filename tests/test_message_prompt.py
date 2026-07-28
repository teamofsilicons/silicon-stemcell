import hashlib
import sys
import types
import unittest
from unittest import mock

from prompts import DNA


class MessagePromptTest(unittest.TestCase):
    def test_message_guide_is_loaded_once_with_exact_contents(self):
        fake_extend = types.ModuleType("core.extend")
        fake_extend.render_manager_catalog = lambda: ""

        with (
            mock.patch.object(DNA, "_get_contact_info", return_value=None),
            mock.patch.object(DNA, "_glass_profile_section", return_value=""),
            mock.patch.object(DNA, "_glass_team_context_section", return_value=""),
            mock.patch.object(DNA, "_glass_trust_policy_section", return_value=""),
            mock.patch.dict(sys.modules, {"core.extend": fake_extend}),
        ):
            prompt = DNA.get_manager_prompt("carbon-1")

        filename = "not_be_ignored.md"
        with open(DNA._prompt_path(filename), encoding="utf-8") as prompt_file:
            exact_contents = prompt_file.read().removesuffix("\n")

        rendered = DNA._read_prompt(filename)
        self.assertEqual(
            hashlib.sha256(exact_contents.encode()).hexdigest(),
            "0f0e7d5f9a4e817ec0ec8d22c6d328cbb289fd613df28eecdee4f942763f84dd",
        )
        self.assertEqual(rendered, f"prompts/{filename}\n{exact_contents}")
        self.assertEqual(prompt.count(f"prompts/{filename}"), 1)
        self.assertIn(rendered, prompt)


if __name__ == "__main__":
    unittest.main()
