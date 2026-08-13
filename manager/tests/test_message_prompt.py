import unittest
from unittest import mock

from prompts import loader as DNA


class MessagePromptTest(unittest.TestCase):
    def test_message_guide_is_loaded_once_and_verbatim(self):
        """The guide reaches the manager whole, once, under its real path.

        The wording is the operator's. What this holds is that the loader does
        not truncate it, mangle it, or pull it in twice.
        """
        with (
            mock.patch.object(DNA, "_get_contact_info", return_value=None),
            mock.patch.object(DNA, "_glass_profile_section", return_value=""),
            mock.patch.object(DNA, "_glass_team_context_section", return_value=""),
            mock.patch.object(DNA, "_glass_trust_policy_section", return_value=""),
        ):
            prompt = DNA.get_manager_prompt()

        filename = "NOT_BE_IGNORED.md"
        path = DNA._prompt_path(filename)
        label = DNA._prompt_label(path)
        with open(path, encoding="utf-8") as prompt_file:
            exact_contents = prompt_file.read().strip()

        rendered = DNA._read_prompt(filename)
        self.assertEqual(rendered, f"{label}\n{exact_contents}")
        # Count the rendered block, not the label: INDEX.md names this file by
        # path too, so the label alone is not unique.
        self.assertEqual(prompt.count(rendered), 1)
        self.assertIn(rendered, prompt)


if __name__ == "__main__":
    unittest.main()
