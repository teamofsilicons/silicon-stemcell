import os
import subprocess
import sys
import json
import tempfile
import time
import unittest
from pathlib import Path

from core import backup, data_policy
from helpers.paths import CODE_ROOT


class SnapshotGarbageCollectionTest(unittest.TestCase):
    def _root(self, directory: str) -> Path:
        root = Path(directory).resolve()
        (root / "prompts").mkdir(parents=True)
        (root / "prompts" / "MEMORY.md").write_text("initial", encoding="utf-8")
        return root

    def _snapshot(
        self,
        root: Path,
        index: int,
    ) -> backup.SnapshotResult:
        (root / "prompts" / "MEMORY.md").write_text(
            f"memory-{index}",
            encoding="utf-8",
        )
        result = backup.create_local_snapshot(
            root,
            release_id=f"release-{index}",
            policy=data_policy.load_data_policy(root, legacy_patterns=[]),
        )
        timestamp = 1_700_000_000_000_000_000 + index
        os.utime(result.manifest_path, ns=(timestamp, timestamp))
        return result

    def _canonical_files(self, root: Path) -> set[Path]:
        store = root / ".silicon" / "snapshots"
        return {
            path
            for path in store.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def test_default_plan_retains_latest_thirty_deterministically(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            snapshots = [self._snapshot(root, index) for index in range(31)]

            first = backup.plan_snapshot_gc(root)
            second = backup.plan_snapshot_gc(root)

            self.assertEqual(first, second)
            self.assertTrue(first.dry_run)
            self.assertEqual(first.retain_latest, 30)
            self.assertEqual(len(first.retained_root_hashes), 30)
            self.assertEqual(
                first.delete_manifests,
                (snapshots[0].manifest_path,),
            )
            self.assertTrue(snapshots[0].manifest_path.exists())

    def test_gc_keeps_latest_and_protected_roots_and_removes_only_orphans(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            snapshots = [self._snapshot(root, index) for index in range(4)]
            protected = snapshots[0].root_hash
            removed = snapshots[1]
            removed_digest = str(removed.manifest["files"][0]["sha256"])
            removed_object = (
                root
                / ".silicon"
                / "snapshots"
                / "objects"
                / "sha256"
                / removed_digest[:2]
                / removed_digest[2:]
            )

            plan = backup.garbage_collect_snapshots(
                root,
                retain_latest=2,
                protected_root_hashes=[protected],
                dry_run=True,
            )

            self.assertEqual(
                set(plan.retained_root_hashes),
                {
                    snapshots[0].root_hash,
                    snapshots[2].root_hash,
                    snapshots[3].root_hash,
                },
            )
            self.assertEqual(plan.delete_manifests, (removed.manifest_path,))
            self.assertEqual(plan.delete_objects, (removed_object,))
            self.assertTrue(removed.manifest_path.exists())
            self.assertTrue(removed_object.exists())

            applied = backup.garbage_collect_snapshots(
                root,
                retain_latest=2,
                protected_root_hashes=[protected],
            )

            self.assertFalse(applied.dry_run)
            self.assertFalse(removed.manifest_path.exists())
            self.assertFalse(removed_object.exists())
            for index in (0, 2, 3):
                self.assertTrue(snapshots[index].manifest_path.exists())

    def test_corrupt_retained_manifest_fails_closed_without_deleting(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            snapshots = [self._snapshot(root, index) for index in range(3)]
            newest = snapshots[-1].manifest_path
            os.chmod(newest, 0o600)
            newest.write_text("{not-json", encoding="utf-8")
            before = self._canonical_files(root)

            with self.assertRaises(backup.SnapshotIntegrityError):
                backup.garbage_collect_snapshots(root, retain_latest=1)

            self.assertEqual(self._canonical_files(root), before)

    def test_corrupt_retained_object_fails_closed_without_deleting(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            snapshots = [self._snapshot(root, index) for index in range(2)]
            entry = snapshots[-1].manifest["files"][0]
            digest = str(entry["sha256"])
            object_path = (
                root
                / ".silicon"
                / "snapshots"
                / "objects"
                / "sha256"
                / digest[:2]
                / digest[2:]
            )
            os.chmod(object_path, 0o600)
            object_path.write_bytes(b"corrupt")
            before = self._canonical_files(root)

            with self.assertRaises(backup.SnapshotIntegrityError):
                backup.garbage_collect_snapshots(root, retain_latest=1)

            self.assertEqual(self._canonical_files(root), before)

    def test_corrupt_expired_manifest_fails_closed_without_deleting(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            snapshots = [self._snapshot(root, index) for index in range(2)]
            expired = snapshots[0].manifest_path
            os.chmod(expired, 0o600)
            expired.write_text("{not-json", encoding="utf-8")
            timestamp = 1_700_000_000_000_000_000
            os.utime(expired, ns=(timestamp, timestamp))
            before = self._canonical_files(root)

            with self.assertRaises(backup.SnapshotIntegrityError):
                backup.garbage_collect_snapshots(
                    root,
                    retain_latest=1,
                )

            self.assertEqual(self._canonical_files(root), before)

    def test_missing_protected_root_fails_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            snapshot = self._snapshot(root, 0)
            before = self._canonical_files(root)

            with self.assertRaises(backup.SnapshotIntegrityError):
                backup.garbage_collect_snapshots(
                    root,
                    retain_latest=0,
                    protected_root_hashes=["f" * 64],
                )

            self.assertEqual(self._canonical_files(root), before)
            self.assertTrue(snapshot.manifest_path.exists())

    def test_unexpected_symlinks_are_never_followed_or_deleted(self):
        with (
            tempfile.TemporaryDirectory() as td,
            tempfile.TemporaryDirectory() as outside_td,
        ):
            root = self._root(td)
            snapshot = self._snapshot(root, 0)
            store = root / ".silicon" / "snapshots"
            outside = Path(outside_td) / "outside"
            outside.write_text("keep", encoding="utf-8")
            prefix = store / "objects" / "sha256" / "aa"
            prefix.mkdir()
            link = prefix / ("b" * 62)
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks are not available")

            plan = backup.garbage_collect_snapshots(
                root,
                retain_latest=1,
                dry_run=True,
            )

            self.assertIn(link, plan.unexpected_entries)
            self.assertNotIn(link, plan.delete_objects)
            with self.assertRaises(backup.SnapshotIntegrityError):
                backup.garbage_collect_snapshots(root, retain_latest=1)
            self.assertTrue(link.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            self.assertTrue(snapshot.manifest_path.exists())

    def test_journal_and_restore_references_protect_every_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            snapshots = [self._snapshot(root, index) for index in range(5)]
            transaction_root = root / ".silicon" / "transactions"
            transaction_root.mkdir()
            now = time.time()
            journal = {
                "schema": 1,
                "transaction_id": "rollback-1",
                "state": "ROLLED_BACK",
                "created_at": now,
                "updated_at": now,
                "metadata": {
                    "operation": "rollback",
                    "recovery_checkpoint": {
                        "root_hash": snapshots[0].root_hash,
                    },
                    "checkpoint_recovery": {
                        "state": "restored",
                        "root_hash": snapshots[0].root_hash,
                    },
                    "candidate_era_preservation": {
                        "state": "verified",
                        "checkpoint": {
                            "root_hash": snapshots[1].root_hash,
                        },
                    },
                },
                "events": [
                    {
                        "state": "CREATED",
                        "at": now,
                        "detail": "created",
                    },
                    {
                        "state": "ROLLED_BACK",
                        "at": now,
                        "detail": "rolled back",
                    },
                ],
            }
            (transaction_root / "rollback-1.json").write_text(
                json.dumps(journal),
                encoding="utf-8",
            )
            (root / backup.IN_PLACE_RESTORE_JOURNAL).write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "root_hash": snapshots[2].root_hash,
                        "state": "APPLYING",
                    }
                ),
                encoding="utf-8",
            )
            (root / backup.IN_PLACE_RESTORE_LATEST).write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "root_hash": snapshots[3].root_hash,
                        "verified_root_hash": snapshots[3].root_hash,
                    }
                ),
                encoding="utf-8",
            )

            protected = backup.discover_protected_snapshot_roots(root)
            plan = backup.garbage_collect_referenced_snapshots(
                root,
                retain_latest=1,
            )

            self.assertEqual(
                set(protected),
                {snapshot.root_hash for snapshot in snapshots[:4]},
            )
            self.assertFalse(plan.delete_manifests)
            for snapshot in snapshots:
                self.assertTrue(snapshot.manifest_path.exists())

    def test_corrupt_update_journal_blocks_gc_before_any_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            self._snapshot(root, 0)
            self._snapshot(root, 1)
            transaction_root = root / ".silicon" / "transactions"
            transaction_root.mkdir()
            (transaction_root / "broken.json").write_text(
                '{"schema":1,"metadata":',
                encoding="utf-8",
            )
            before = self._canonical_files(root)

            with self.assertRaises(backup.SnapshotIntegrityError):
                protected = backup.discover_protected_snapshot_roots(root)
                backup.garbage_collect_snapshots(
                    root,
                    retain_latest=1,
                    protected_root_hashes=protected,
                )

            self.assertEqual(self._canonical_files(root), before)

    def test_redirected_snapshot_store_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as td,
            tempfile.TemporaryDirectory() as outside_td,
        ):
            root = self._root(td)
            (root / ".silicon").mkdir()
            try:
                (root / ".silicon" / "snapshots").symlink_to(
                    Path(outside_td),
                    target_is_directory=True,
                )
            except OSError:
                self.skipTest("symlinks are not available")

            with self.assertRaises(data_policy.UnsafePathError):
                backup.plan_snapshot_gc(root)

    def test_scan_limit_fails_before_deleting_anything(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            self._snapshot(root, 0)
            self._snapshot(root, 1)
            before = self._canonical_files(root)

            with self.assertRaises(backup.SnapshotLimitError):
                backup.garbage_collect_snapshots(
                    root,
                    retain_latest=0,
                    gc_limits=backup.SnapshotGCLimits(max_manifests=1),
                )

            self.assertEqual(self._canonical_files(root), before)

    @unittest.skipUnless(backup.fcntl is not None, "requires Unix flock")
    def test_snapshot_creation_and_gc_share_a_cross_process_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            self._snapshot(root, 0)
            script = "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from core import backup",
                    "print('ready', flush=True)",
                    "backup.plan_snapshot_gc(Path(sys.argv[1]))",
                    "print('done', flush=True)",
                )
            )
            with backup._snapshot_store_lock(root):
                process = subprocess.Popen(
                    [sys.executable, "-c", script, str(root)],
                    cwd=CODE_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(
                    lambda: process.kill() if process.poll() is None else None
                )
                self.assertIsNotNone(process.stdout)
                self.assertEqual(process.stdout.readline().strip(), "ready")
                time.sleep(0.15)
                self.assertIsNone(process.poll())

            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "done")


if __name__ == "__main__":
    unittest.main()
