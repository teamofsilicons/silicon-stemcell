import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import backup
from core import living_files


class BackupManifestTest(unittest.TestCase):
    def test_legacy_backupsilicon_directory_is_archived_and_manifest_restored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".backupsilicon").mkdir()
            (root / ".backupsilicon" / "old.txt").write_text(
                "legacy backup", encoding="utf-8"
            )

            with mock.patch.object(
                backup.time,
                "strftime",
                return_value="20260101T000000Z",
            ):
                archived = backup.ensure_manifest_file(root)

            self.assertEqual(archived, [".backupsilicon.archive.20260101T000000Z"])
            self.assertTrue((root / ".backupsilicon").is_file())
            self.assertEqual(backup.read_manifest(root), [])
            self.assertEqual(
                (
                    root / ".backupsilicon.archive.20260101T000000Z" / "old.txt"
                ).read_text(encoding="utf-8"),
                "legacy backup",
            )

    def test_legacy_backupsilicon_directory_in_non_git_install_gets_default_manifest(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".backupsilicon").mkdir()
            (root / ".backupsilicon" / "old.txt").write_text(
                "legacy backup", encoding="utf-8"
            )

            with mock.patch.object(
                backup.time,
                "strftime",
                return_value="20260101T000000Z",
            ):
                archived = backup.ensure_manifest_file(root)

            self.assertEqual(archived, [".backupsilicon.archive.20260101T000000Z"])
            self.assertTrue((root / ".backupsilicon").is_file())
            self.assertEqual(backup.read_manifest(root), [])

    def test_missing_manifest_gets_the_canonical_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(backup.ensure_manifest_file(root), [])
            self.assertEqual(
                backup.read_manifest(root),
                list(backup.DEFAULT_MANIFEST),
            )


class LivingFilesTest(unittest.TestCase):
    def test_seed_only_creates_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            templates = root / "templates"
            (templates / "prompts").mkdir(parents=True)
            (templates / "prompts" / "MEMORY.md").write_text(
                "template",
                encoding="utf-8",
            )
            (root / "prompts").mkdir()
            (root / "prompts" / "MEMORY.md").write_text(
                "living",
                encoding="utf-8",
            )
            (templates / "prompts" / "LORE.md").write_text(
                "lore",
                encoding="utf-8",
            )

            seeded = living_files.seed_living_files(root, templates)

            self.assertEqual(seeded, ["prompts/LORE.md"])
            self.assertEqual(
                (root / "prompts" / "MEMORY.md").read_text(encoding="utf-8"),
                "living",
            )
            self.assertEqual(
                (root / "prompts" / "LORE.md").read_text(encoding="utf-8"),
                "lore",
            )


if __name__ == "__main__":
    unittest.main()
