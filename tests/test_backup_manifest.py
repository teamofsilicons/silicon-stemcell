import tempfile
import unittest
from pathlib import Path
from unittest import mock

from interface import backup


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


if __name__ == "__main__":
    unittest.main()


class AppendingSourceSnapshotTest(unittest.TestCase):
    """A running silicon must stay snapshotable while it writes.

    The pre-update backup used to demand a byte-identical stat before and after
    copying. A live interface inbox is appended to continuously, so every retry
    lost the race, the snapshot failed, and the whole update rolled back.
    """

    def test_a_concurrent_append_does_not_invalidate_the_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "inbox.jsonl"
            path.write_bytes(b'{"a":1}\n')
            before = path.stat()
            with path.open("ab") as handle:  # the silicon keeps writing
                handle.write(b'{"b":2}\n')
            after = path.stat()

            self.assertNotEqual(before.st_size, after.st_size)
            self.assertTrue(backup._source_only_appended(before, after))

    def test_a_replaced_file_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_bytes(b"original")
            before = path.stat()
            replacement = Path(td) / "new.json"
            replacement.write_bytes(b"rewritten")
            replacement.replace(path)  # new inode
            after = path.stat()

            self.assertFalse(backup._source_only_appended(before, after))

    def test_a_truncated_file_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "log.txt"
            path.write_bytes(b"aaaaaaaaaaaaaaaa")
            before = path.stat()
            with path.open("r+b") as handle:
                handle.truncate(4)
            after = path.stat()

            self.assertFalse(backup._source_only_appended(before, after))
