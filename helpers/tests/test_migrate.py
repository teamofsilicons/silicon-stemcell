"""The on-disk migration must never lose a live Silicon's state."""
import tempfile
import unittest
from pathlib import Path

from helpers.migrate import MARKER, copy_file_once, copy_tree_once


class CopyTreeOnceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.legacy = self.root / "core" / "interface_state"
        self.current = self.root / "interface" / "state"
        (self.legacy / "sub").mkdir(parents=True)
        (self.legacy / "contacts.json").write_text("{}", encoding="utf-8")
        (self.legacy / "sub" / "trust.json").write_text("[]", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_the_whole_tree_is_copied_and_the_original_is_left_alone(self):
        self.assertTrue(copy_tree_once(self.legacy, self.current))

        self.assertEqual(
            (self.current / "contacts.json").read_text(encoding="utf-8"), "{}"
        )
        self.assertEqual(
            (self.current / "sub" / "trust.json").read_text(encoding="utf-8"), "[]"
        )
        self.assertTrue((self.legacy / "contacts.json").is_file())

    def test_a_second_run_does_nothing(self):
        copy_tree_once(self.legacy, self.current)
        (self.current / "contacts.json").write_text('{"live": true}', encoding="utf-8")

        self.assertFalse(copy_tree_once(self.legacy, self.current))
        self.assertEqual(
            (self.current / "contacts.json").read_text(encoding="utf-8"),
            '{"live": true}',
        )

    def test_a_marked_legacy_directory_is_never_copied_again(self):
        (self.legacy / MARKER).write_text("done\n", encoding="utf-8")

        self.assertFalse(copy_tree_once(self.legacy, self.current))
        self.assertFalse(self.current.exists())

    def test_nothing_happens_without_a_legacy_directory(self):
        self.assertFalse(
            copy_tree_once(self.root / "absent", self.root / "elsewhere")
        )


class CopyFileOnceTest(unittest.TestCase):
    def test_the_new_name_is_created_once_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / ".glass.json"
            current = root / ".interface.json"
            legacy.write_text('{"api_key": "old"}', encoding="utf-8")

            self.assertTrue(copy_file_once(legacy, current))
            self.assertEqual(
                current.read_text(encoding="utf-8"), '{"api_key": "old"}'
            )

            current.write_text('{"api_key": "new"}', encoding="utf-8")
            self.assertFalse(copy_file_once(legacy, current))
            self.assertEqual(
                current.read_text(encoding="utf-8"), '{"api_key": "new"}'
            )


if __name__ == "__main__":
    unittest.main()
