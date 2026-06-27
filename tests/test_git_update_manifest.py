import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import git_update


class GitUpdateManifestTest(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_legacy_backupsilicon_directory_is_archived_and_manifest_restored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._git(root, "init")
            self._git(root, "config", "user.name", "test")
            self._git(root, "config", "user.email", "test@example.com")
            (root / ".backupsilicon").write_text("prompts/MEMORY.md\n", encoding="utf-8")
            self._git(root, "add", ".backupsilicon")
            self._git(root, "commit", "-m", "seed manifest")

            (root / ".backupsilicon").unlink()
            (root / ".backupsilicon").mkdir()
            (root / ".backupsilicon" / "old.txt").write_text("legacy backup", encoding="utf-8")

            with mock.patch.object(git_update, "PROJECT_ROOT", root), mock.patch.object(
                git_update.time, "strftime", return_value="20260101T000000Z"
            ):
                archived = git_update.ensure_manifest_file()

            self.assertEqual(archived, [".backupsilicon.archive.20260101T000000Z"])
            self.assertTrue((root / ".backupsilicon").is_file())
            self.assertEqual((root / ".backupsilicon").read_text(encoding="utf-8"), "prompts/MEMORY.md\n")
            self.assertEqual(
                (root / ".backupsilicon.archive.20260101T000000Z" / "old.txt").read_text(encoding="utf-8"),
                "legacy backup",
            )

    def test_legacy_backupsilicon_directory_in_non_git_install_gets_default_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".backupsilicon").mkdir()
            (root / ".backupsilicon" / "old.txt").write_text("legacy backup", encoding="utf-8")

            with mock.patch.object(git_update, "PROJECT_ROOT", root), mock.patch.object(
                git_update.time, "strftime", return_value="20260101T000000Z"
            ):
                archived = git_update.ensure_manifest_file()

            self.assertEqual(archived, [".backupsilicon.archive.20260101T000000Z"])
            self.assertTrue((root / ".backupsilicon").is_file())
            self.assertIn("prompts/MEMORY.md", (root / ".backupsilicon").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
