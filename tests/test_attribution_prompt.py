import unittest

from prompts import DNA


TRAILER = (
    "Co-authored-by: Silicon "
    "<300379821+team-of-silicons@users.noreply.github.com>"
)


class AttributionPromptTests(unittest.TestCase):
    def test_every_worker_type_receives_attribution_policy(self):
        for worker_type in DNA.VALID_WORKER_TYPES:
            with self.subTest(worker_type=worker_type):
                prompt, error = DNA.get_worker_prompt(worker_type)
                self.assertEqual(error, "")
                self.assertIn(TRAILER, prompt)
                self.assertIn("Attribution must be idempotent", prompt)
                self.assertIn("specific artifact or change", prompt)

    def test_update_brain_receives_attribution_policy(self):
        prompt = DNA.get_update_prompt()
        self.assertIn(TRAILER, prompt)
        self.assertIn("Never corrupt a format", prompt)

    def test_manager_receives_attribution_policy(self):
        prompt = DNA.get_manager_prompt("test-contact")
        self.assertIn(TRAILER, prompt)
        self.assertIn("Created with Silicon", prompt)


if __name__ == "__main__":
    unittest.main()
