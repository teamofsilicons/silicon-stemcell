import hashlib
import json
import os
import socket
import stat
import tarfile
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from interface import backup

from interface.backup import policy as data_policy
from helpers import state as state_store


def write_release_floor(
    root: Path,
    sequence: int,
    tree_sha256: str,
) -> None:
    path = root / backup.RELEASE_SEQUENCE_FLOOR
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "sequence": sequence,
                "tree_sha256": tree_sha256,
                "recorded_at": float(sequence),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def write_release_floor_v2(
    root: Path,
    version: str,
    tree_sha256: str,
) -> None:
    major, minor, patch = (int(part) for part in version.split("."))
    sequence = major * 1_000_000 + minor * 1_000 + patch + 1
    path = root / backup.RELEASE_SEQUENCE_FLOOR
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 2,
                "sequence": sequence,
                "version": version,
                "trust": "git-semver-tag",
                "tree_sha256": tree_sha256,
                "recorded_at": float(sequence),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


class DataPolicyTest(unittest.TestCase):
    def test_mandatory_classes_survive_local_additions_and_legacy_import(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".silicon").mkdir()
            (root / ".silicon" / "data-policy.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "additive": {
                            "critical_living": ["custom/private-memory.md"],
                            "artifacts": ["deliverables/**"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            policy = data_policy.load_data_policy(
                root,
                legacy_patterns=["prompts/MEMORY.md", "legacy/**"],
            )

            self.assertIn(
                "prompts/MEMORY.md",
                policy.classes["critical_living"],
            )
            self.assertIn(
                "custom/private-memory.md",
                policy.classes["critical_living"],
            )
            self.assertEqual(policy.additive["legacy_additive"], ("legacy/**",))
            self.assertNotIn("credentials", policy.classes)

    def test_policy_cannot_redefine_or_disable_mandatory_classes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".silicon").mkdir()
            path = root / ".silicon" / "data-policy.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "mandatory": {},
                        "additive": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(data_policy.DataPolicyError):
                data_policy.load_data_policy(root, legacy_patterns=[])

            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "additive": {"credentials": [".glass.json"]},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(data_policy.DataPolicyError):
                data_policy.load_data_policy(root, legacy_patterns=[])

    def test_patterns_reject_absolute_traversal_and_windows_paths(self):
        invalid = (
            "/etc/passwd",
            "../outside",
            "safe/../../outside",
            r"C:\Users\secret",
            r"safe\file.txt",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(data_policy.DataPolicyError):
                    data_policy.validate_relative_pattern(value)

    def test_selected_symlink_is_rejected_without_reading_target(self):
        with (
            tempfile.TemporaryDirectory() as td,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = Path(td)
            secret = Path(outside) / "secret.txt"
            secret.write_text("must not leak", encoding="utf-8")
            (root / "custom").mkdir()
            link = root / "custom" / "leak.txt"
            try:
                link.symlink_to(secret)
            except OSError:
                self.skipTest("symlinks are not available")

            policy = data_policy.load_data_policy(
                root,
                legacy_patterns=["custom/**"],
            )
            with self.assertRaises(data_policy.UnsafePathError):
                policy.resolve(root)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is not available")
    def test_selected_special_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "runtime").mkdir()
            os.mkfifo(root / "runtime" / "pipe")
            policy = data_policy.load_data_policy(
                root,
                legacy_patterns=["runtime/**"],
            )
            with self.assertRaises(data_policy.UnsafePathError):
                policy.resolve(root)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets unavailable")
    def test_runtime_interface_socket_is_excluded_but_state_is_protected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / ".silicon-interface"
            state.mkdir()
            durable = state / "delivery-state.json"
            durable.write_text('{"cursor": 7}\n', encoding="utf-8")
            daemon_socket = state / "daemon.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(daemon_socket))
            finally:
                listener.close()

            policy = data_policy.load_data_policy(root, legacy_patterns=[])
            resolved = {item.relative_path for item in policy.resolve(root)}

            self.assertIn(".silicon-interface/delivery-state.json", resolved)
            self.assertNotIn(".silicon-interface/daemon.sock", resolved)
            snapshot = backup.create_local_snapshot(
                root,
                release_id="release-with-interface-socket",
                policy=policy,
            )
            paths = {entry["path"] for entry in snapshot.manifest["files"]}
            self.assertIn(".silicon-interface/delivery-state.json", paths)
            self.assertNotIn(".silicon-interface/daemon.sock", paths)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets unavailable")
    def test_unrecognized_runtime_socket_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "runtime"
            runtime.mkdir()
            other_socket = runtime / "other.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(other_socket))
            finally:
                listener.close()

            policy = data_policy.load_data_policy(
                root,
                legacy_patterns=["runtime/**"],
            )
            with self.assertRaises(data_policy.UnsafePathError):
                policy.resolve(root)

    def test_regular_file_named_like_runtime_socket_remains_protected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / ".silicon-interface"
            state.mkdir()
            daemon_file = state / "daemon.sock"
            daemon_file.write_text("durable", encoding="utf-8")

            policy = data_policy.load_data_policy(root, legacy_patterns=[])
            resolved = {item.relative_path for item in policy.resolve(root)}

            self.assertIn(".silicon-interface/daemon.sock", resolved)

    def test_broad_addition_cannot_capture_plaintext_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".glass.json").write_text("secret", encoding="utf-8")
            policy = data_policy.load_data_policy(root, legacy_patterns=["**"])

            with self.assertRaises(data_policy.UnsafePathError):
                policy.resolve(root)

    def test_legacy_manifest_is_additive_not_a_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".backupsilicon").write_text(
                "custom/knowledge.txt\n",
                encoding="utf-8",
            )
            (root / "custom").mkdir()
            (root / "custom" / "knowledge.txt").write_text(
                "custom",
                encoding="utf-8",
            )
            (root / "prompts").mkdir()
            (root / "prompts" / "MEMORY.md").write_text(
                "mandatory",
                encoding="utf-8",
            )

            resolved = {
                item.relative_path for item in backup.load_policy(root).resolve(root)
            }

            self.assertIn("custom/knowledge.txt", resolved)
            self.assertIn("prompts/MEMORY.md", resolved)

    def test_generated_legacy_defaults_are_removed_but_custom_additions_remain(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".backupsilicon").write_text(
                "\n".join(
                    (
                        "# keep this explanation",
                        *backup.LEGACY_DEFAULT_MANIFEST,
                        "custom/knowledge.txt",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(
                backup.read_manifest(root),
                ["custom/knowledge.txt"],
            )
            migrated = (root / ".backupsilicon").read_text(encoding="utf-8")
            self.assertIn(backup.MANIFEST_HEADER, migrated)
            self.assertIn("# keep this explanation", migrated)
            self.assertIn("custom/knowledge.txt", migrated)
            self.assertNotIn("prompts/MEMORY.md", migrated)

    def test_maintenance_queue_and_leases_are_mandatory_task_delivery_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "core" / "interface_state"
            state.mkdir(parents=True)
            (state / "maintenance.json").write_text(
                json.dumps(
                    {
                        "epoch": 3,
                        "queued_roots": [{"contact_id": "carbon-a"}],
                        "leases": {"lease-a": {"kind": "manager"}},
                    }
                ),
                encoding="utf-8",
            )

            protected = {
                item.relative_path: item.classes
                for item in data_policy.load_data_policy(
                    root,
                    legacy_patterns=[],
                ).resolve(root)
            }

            self.assertEqual(
                protected["core/interface_state/maintenance.json"],
                ("task_delivery",),
            )

    def test_atomic_state_write_temporaries_are_not_protected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "core" / "interface_state"
            state.mkdir(parents=True)
            (state / "maintenance.json").write_text("{}", encoding="utf-8")
            atomic_temporary = state / ".maintenance.json.26.133064281805504.tmp"
            atomic_temporary.write_text("incomplete", encoding="utf-8")
            tempfile_temporary = state / ".maintenance.json.jl_0y4vb.tmp"
            tempfile_temporary.write_text("incomplete", encoding="utf-8")
            ordinary_temporary = state / "operator-notes.tmp"
            ordinary_temporary.write_text("keep", encoding="utf-8")

            protected = {
                item.relative_path
                for item in data_policy.load_data_policy(
                    root,
                    legacy_patterns=[],
                ).resolve(root)
            }

            self.assertIn("core/interface_state/maintenance.json", protected)
            self.assertIn("core/interface_state/operator-notes.tmp", protected)
            self.assertNotIn(
                "core/interface_state/.maintenance.json.26.133064281805504.tmp",
                protected,
            )
            self.assertNotIn(
                "core/interface_state/.maintenance.json.jl_0y4vb.tmp",
                protected,
            )

    def test_release_sequence_floor_is_mandatory_security_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_release_floor(root, 7, "a" * 64)

            protected = {
                item.relative_path: item.classes
                for item in data_policy.load_data_policy(
                    root,
                    legacy_patterns=[],
                ).resolve(root)
            }

            self.assertEqual(
                protected[backup.RELEASE_SEQUENCE_FLOOR],
                ("security_state",),
            )


class LocalSnapshotTest(unittest.TestCase):
    def _root_with_memory(self, td: str, content: str = "remember") -> Path:
        root = Path(td)
        (root / "prompts").mkdir(parents=True)
        (root / "prompts" / "MEMORY.md").write_text(content, encoding="utf-8")
        os.chmod(root / "prompts" / "MEMORY.md", 0o640)
        return root

    def _policy(self, root: Path) -> data_policy.DataPolicy:
        return data_policy.load_data_policy(root, legacy_patterns=[])

    def test_manifest_is_deterministic_and_content_addressed(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            policy = self._policy(root)

            first = backup.create_local_snapshot(
                root,
                release_id="release-7",
                policy=policy,
            )
            first_bytes = first.manifest_path.read_bytes()
            second = backup.create_local_snapshot(
                root,
                release_id="release-7",
                policy=policy,
            )

            self.assertEqual(first.root_hash, second.root_hash)
            self.assertEqual(first_bytes, second.manifest_path.read_bytes())
            entry = first.manifest["files"][0]
            self.assertEqual(entry["path"], "prompts/MEMORY.md")
            self.assertEqual(entry["size"], len(b"remember"))
            self.assertEqual(entry["mode"], 0o640)
            self.assertEqual(
                entry["sha256"],
                hashlib.sha256(b"remember").hexdigest(),
            )
            self.assertIn("critical_living", entry["classes"])
            self.assertIn("self_customization", entry["classes"])
            self.assertEqual(
                len(list((root / ".silicon" / "snapshots" / "manifests").iterdir())), 1
            )

    def test_snapshot_uses_maintenance_writer_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "core" / "interface_state"
            state.mkdir(parents=True)
            maintenance = state / "maintenance.json"
            maintenance.write_text('{"phase":"draining"}', encoding="utf-8")
            attempted = threading.Event()
            finished = threading.Event()
            results = []
            errors = []
            original_lock = backup.state_file_lock

            @contextmanager
            def tracking_lock(path):
                if Path(path).resolve() == maintenance.resolve():
                    attempted.set()
                with original_lock(path):
                    yield

            def create_snapshot():
                try:
                    results.append(
                        backup.create_local_snapshot(
                            root,
                            release_id="release-locked",
                            policy=self._policy(root),
                        )
                    )
                except Exception as exc:
                    errors.append(exc)
                finally:
                    finished.set()

            with mock.patch.object(
                backup.store,
                "state_file_lock",
                tracking_lock,
            ), state_store.file_lock(maintenance):
                worker = threading.Thread(target=create_snapshot)
                worker.start()
                self.assertTrue(attempted.wait(timeout=2))
                self.assertFalse(finished.wait(timeout=0.1))

            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(
                next(
                    item["path"]
                    for item in results[0].manifest["files"]
                    if item["path"] == backup.MAINTENANCE_STATE
                ),
                backup.MAINTENANCE_STATE,
            )

    def test_release_identity_is_part_of_the_root_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            policy = self._policy(root)
            first = backup.create_local_snapshot(
                root,
                release_id="release-a",
                policy=policy,
            )
            second = backup.create_local_snapshot(
                root,
                release_id="release-b",
                policy=policy,
            )
            self.assertNotEqual(first.root_hash, second.root_hash)

    def test_installed_release_identity_comes_from_generation_pointer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".silicon").mkdir()
            (root / ".silicon" / "current.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "generation_id": "generation-abc",
                        "release": {"tree_sha256": "f" * 64},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                backup.installed_release_id(root),
                "generation-abc",
            )

    def test_tombstone_records_a_previously_protected_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            policy = self._policy(root)
            first = backup.create_local_snapshot(
                root,
                release_id="release-1",
                policy=policy,
            )
            (root / "prompts" / "MEMORY.md").unlink()
            second = backup.create_local_snapshot(
                root,
                release_id="release-2",
                policy=policy,
                previous_manifest=first.manifest,
            )

            self.assertEqual(
                second.manifest["tombstones"],
                ["prompts/MEMORY.md"],
            )

    def test_verification_detects_object_corruption(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            result = backup.create_local_snapshot(
                root,
                release_id="release-1",
                policy=self._policy(root),
            )
            entry = result.manifest["files"][0]
            object_path = (
                root
                / ".silicon"
                / "snapshots"
                / "objects"
                / "sha256"
                / entry["sha256"][:2]
                / entry["sha256"][2:]
            )
            os.chmod(object_path, 0o600)
            object_path.write_bytes(b"corrupt")

            with self.assertRaises(backup.SnapshotIntegrityError):
                backup.verify_local_snapshot(
                    result.root_hash,
                    store=root / ".silicon" / "snapshots",
                )

    def test_limits_fail_closed_instead_of_skipping_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td, "0123456789")
            with self.assertRaises(backup.SnapshotLimitError):
                backup.create_local_snapshot(
                    root,
                    release_id="release-1",
                    policy=self._policy(root),
                    limits=backup.SnapshotLimits(
                        max_files=10,
                        max_file_size=5,
                        max_total_size=100,
                        chunk_size=4096,
                    ),
                )

    def test_snapshot_rejects_invalid_release_sequence_floor(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            write_release_floor(root, 0, "a" * 64)

            with self.assertRaisesRegex(
                backup.SnapshotIntegrityError,
                "release sequence floor.*invalid",
            ):
                backup.create_local_snapshot(
                    root,
                    release_id="release-1",
                    policy=self._policy(root),
                )

    def test_snapshot_accepts_current_versioned_release_floor(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            write_release_floor_v2(root, "2.0.1", "a" * 64)

            result = backup.create_local_snapshot(
                root,
                release_id="release-1",
                policy=self._policy(root),
            )

            floor_entry = next(
                entry
                for entry in result.manifest["files"]
                if entry["path"] == backup.RELEASE_SEQUENCE_FLOOR
            )
            self.assertEqual(floor_entry["classes"], ["security_state"])

    def test_snapshot_rejects_versioned_floor_with_wrong_sequence(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            write_release_floor_v2(root, "2.0.1", "a" * 64)
            path = root / backup.RELEASE_SEQUENCE_FLOOR
            value = json.loads(path.read_text(encoding="utf-8"))
            value["sequence"] += 1
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                backup.SnapshotIntegrityError,
                "release sequence floor.*invalid",
            ):
                backup.create_local_snapshot(
                    root,
                    release_id="release-1",
                    policy=self._policy(root),
                )

    def test_default_snapshot_store_cannot_be_redirected_by_symlink(self):
        with (
            tempfile.TemporaryDirectory() as td,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = self._root_with_memory(td)
            policy = self._policy(root)
            try:
                (root / ".silicon").symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("symlinks are not available")

            with self.assertRaises(data_policy.UnsafePathError):
                backup.create_local_snapshot(
                    root,
                    release_id="release-1",
                    policy=policy,
                )

    def test_restore_dry_run_writes_nothing_then_restores_exactly(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            root = self._root_with_memory(str(workspace / "source"))
            result = backup.create_local_snapshot(
                root,
                release_id="release-1",
                policy=self._policy(root),
            )
            store = root / ".silicon" / "snapshots"
            target = workspace / "restored"

            dry_run = backup.restore_snapshot(
                result.root_hash,
                target,
                store=store,
                dry_run=True,
            )
            self.assertTrue(dry_run.dry_run)
            self.assertFalse(target.exists())
            restored = backup.restore_snapshot(
                result.manifest_path,
                target,
                store=store,
            )

            self.assertFalse(restored.dry_run)
            restored_file = target / "prompts" / "MEMORY.md"
            self.assertEqual(restored_file.read_text(encoding="utf-8"), "remember")
            self.assertEqual(stat.S_IMODE(restored_file.stat().st_mode), 0o640)
            with self.assertRaises(backup.SnapshotError):
                backup.restore_snapshot(result.root_hash, target, store=store)

    def test_in_place_restore_is_exact_for_owned_paths_and_preserves_unknown_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td, "snapshot memory")
            (root / "prompts" / "obsolete.md").write_text(
                "remove me",
                encoding="utf-8",
            )
            policy = self._policy(root)
            first = backup.create_local_snapshot(
                root,
                release_id="release-1",
                policy=policy,
            )
            (root / "prompts" / "obsolete.md").unlink()
            second = backup.create_local_snapshot(
                root,
                release_id="release-2",
                policy=policy,
                previous_manifest=first.manifest,
            )
            store = root / ".silicon" / "snapshots"

            (root / "prompts" / "MEMORY.md").write_text(
                "candidate mutation",
                encoding="utf-8",
            )
            (root / "prompts" / "obsolete.md").write_text(
                "candidate resurrected this",
                encoding="utf-8",
            )
            (root / "unknown-after-snapshot.txt").write_text(
                "preserve",
                encoding="utf-8",
            )
            (root / ".silicon" / "current.json").write_text(
                '{"generation_id":"candidate"}\n',
                encoding="utf-8",
            )
            maintenance = root / ".silicon" / "maintenance" / "lease.json"
            maintenance.parent.mkdir()
            maintenance.write_text('{"active":true}\n', encoding="utf-8")

            restored = backup.restore_local_snapshot_in_place(
                root,
                second.root_hash,
                store=store,
            )

            self.assertEqual(restored.root_hash, second.root_hash)
            self.assertEqual(
                (root / "prompts" / "MEMORY.md").read_text(encoding="utf-8"),
                "snapshot memory",
            )
            self.assertFalse((root / "prompts" / "obsolete.md").exists())
            self.assertEqual(
                (root / "unknown-after-snapshot.txt").read_text(encoding="utf-8"),
                "preserve",
            )
            self.assertEqual(
                (root / ".silicon" / "current.json").read_text(encoding="utf-8"),
                '{"generation_id":"candidate"}\n',
            )
            self.assertEqual(
                maintenance.read_text(encoding="utf-8"),
                '{"active":true}\n',
            )
            latest = json.loads(
                (root / backup.IN_PLACE_RESTORE_LATEST).read_text(encoding="utf-8")
            )
            self.assertEqual(latest["root_hash"], second.root_hash)
            self.assertEqual(latest["verified_root_hash"], second.root_hash)

            # Repeating a committed restore repairs later mutations too.
            (root / "prompts" / "MEMORY.md").write_text(
                "mutated again",
                encoding="utf-8",
            )
            backup.restore_local_snapshot_in_place(
                root,
                second.manifest_path,
                store=store,
            )
            self.assertEqual(
                (root / "prompts" / "MEMORY.md").read_text(encoding="utf-8"),
                "snapshot memory",
            )

    def test_in_place_restore_never_lowers_release_sequence_floor(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td, "snapshot memory")
            write_release_floor(root, 10, "a" * 64)
            result = backup.create_local_snapshot(
                root,
                release_id="release-10",
                policy=self._policy(root),
            )
            floor_entry = next(
                entry
                for entry in result.manifest["files"]
                if entry["path"] == backup.RELEASE_SEQUENCE_FLOOR
            )
            self.assertEqual(floor_entry["classes"], ["security_state"])

            (root / "prompts" / "MEMORY.md").write_text(
                "candidate mutation",
                encoding="utf-8",
            )
            write_release_floor(root, 11, "b" * 64)

            backup.restore_local_snapshot_in_place(
                root,
                result.root_hash,
                store=root / ".silicon" / "snapshots",
            )

            self.assertEqual(
                (root / "prompts" / "MEMORY.md").read_text(encoding="utf-8"),
                "snapshot memory",
            )
            floor = json.loads(
                (root / backup.RELEASE_SEQUENCE_FLOOR).read_text(encoding="utf-8")
            )
            self.assertEqual(floor["sequence"], 11)
            self.assertEqual(floor["tree_sha256"], "b" * 64)

    def test_in_place_restore_raises_release_sequence_floor(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            write_release_floor(root, 11, "b" * 64)
            result = backup.create_local_snapshot(
                root,
                release_id="release-11",
                policy=self._policy(root),
            )
            write_release_floor(root, 10, "a" * 64)

            backup.restore_local_snapshot_in_place(
                root,
                result.root_hash,
                store=root / ".silicon" / "snapshots",
            )

            floor = json.loads(
                (root / backup.RELEASE_SEQUENCE_FLOOR).read_text(encoding="utf-8")
            )
            self.assertEqual(floor["sequence"], 11)
            self.assertEqual(floor["tree_sha256"], "b" * 64)

    def test_restore_serializes_release_floor_with_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            write_release_floor(root, 11, "b" * 64)
            result = backup.create_local_snapshot(
                root,
                release_id="release-11",
                policy=self._policy(root),
            )
            write_release_floor(root, 10, "a" * 64)

            restore_entered = threading.Event()
            finish_restore = threading.Event()
            writer_started = threading.Event()
            writer_done = threading.Event()
            errors: list[BaseException] = []
            original_restore = backup.restore._restore_local_snapshot_in_place_locked

            def blocked_restore(*args, **kwargs):
                restore_entered.set()
                if not finish_restore.wait(2):
                    raise AssertionError("test did not release snapshot restore")
                return original_restore(*args, **kwargs)

            def run_restore() -> None:
                try:
                    backup.restore_local_snapshot_in_place(
                        root,
                        result.root_hash,
                        store=root / ".silicon" / "snapshots",
                    )
                except BaseException as exc:
                    errors.append(exc)

            def run_writer() -> None:
                writer_started.set()
                try:
                    with backup._release_floor_lock(root):
                        write_release_floor(root, 12, "c" * 64)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    writer_done.set()

            with mock.patch.object(
                backup.restore,
                "_restore_local_snapshot_in_place_locked",
                side_effect=blocked_restore,
            ):
                restore_thread = threading.Thread(target=run_restore)
                restore_thread.start()
                self.assertTrue(restore_entered.wait(2))
                writer_thread = threading.Thread(target=run_writer)
                writer_thread.start()
                self.assertTrue(writer_started.wait(2))
                self.assertFalse(writer_done.wait(0.1))
                finish_restore.set()
                restore_thread.join(2)
                writer_thread.join(2)

            self.assertFalse(restore_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
            self.assertEqual(errors, [])
            floor = json.loads(
                (root / backup.RELEASE_SEQUENCE_FLOOR).read_text(encoding="utf-8")
            )
            self.assertEqual(floor["sequence"], 12)
            self.assertEqual(floor["tree_sha256"], "c" * 64)

    def test_release_floor_lock_rejects_path_replacement_during_acquire(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td)
            lock_path = root / backup.RELEASE_SEQUENCE_FLOOR_LOCK
            original_lock = backup.locks.lock_handle

            def replace_after_kernel_lock(handle) -> None:
                original_lock(handle)
                replacement = lock_path.with_name(
                    f".{lock_path.name}.replacement"
                )
                replacement.write_bytes(b"replacement")
                os.chmod(replacement, 0o600)
                os.replace(replacement, lock_path)

            with mock.patch.object(
                backup.locks,
                "lock_handle",
                side_effect=replace_after_kernel_lock,
            ):
                with self.assertRaisesRegex(
                    backup.SnapshotIntegrityError,
                    "changed while it was being acquired",
                ):
                    with backup._release_floor_lock(root):
                        self.fail("unsafe replacement lock was accepted")

    def test_in_place_restore_rejects_equal_sequence_tree_conflict_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td, "snapshot memory")
            write_release_floor(root, 11, "a" * 64)
            result = backup.create_local_snapshot(
                root,
                release_id="release-11",
                policy=self._policy(root),
            )
            (root / "prompts" / "MEMORY.md").write_text(
                "must remain untouched",
                encoding="utf-8",
            )
            write_release_floor(root, 11, "b" * 64)

            with self.assertRaisesRegex(
                backup.SnapshotIntegrityError,
                "one sequence for different immutable release trees",
            ):
                backup.restore_local_snapshot_in_place(
                    root,
                    result.root_hash,
                    store=root / ".silicon" / "snapshots",
                )

            self.assertEqual(
                (root / "prompts" / "MEMORY.md").read_text(encoding="utf-8"),
                "must remain untouched",
            )
            self.assertFalse((root / backup.IN_PLACE_RESTORE_JOURNAL).exists())

    def test_release_sequence_floor_tombstone_is_never_applied(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td, "snapshot memory")
            write_release_floor(root, 10, "a" * 64)
            first = backup.create_local_snapshot(
                root,
                release_id="release-10",
                policy=self._policy(root),
            )
            (root / backup.RELEASE_SEQUENCE_FLOOR).unlink()
            second = backup.create_local_snapshot(
                root,
                release_id="release-without-floor",
                policy=self._policy(root),
                previous_manifest=first.manifest,
            )
            self.assertIn(
                backup.RELEASE_SEQUENCE_FLOOR,
                second.manifest["tombstones"],
            )
            write_release_floor(root, 11, "b" * 64)
            (root / "prompts" / "MEMORY.md").write_text(
                "candidate mutation",
                encoding="utf-8",
            )

            backup.restore_local_snapshot_in_place(
                root,
                second.root_hash,
                store=root / ".silicon" / "snapshots",
            )

            floor = json.loads(
                (root / backup.RELEASE_SEQUENCE_FLOOR).read_text(encoding="utf-8")
            )
            self.assertEqual(floor["sequence"], 11)
            self.assertEqual(floor["tree_sha256"], "b" * 64)

    def test_in_place_restore_resumes_its_durable_operation_journal(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root_with_memory(td, "memory")
            (root / "prompts" / "LORE.md").write_text(
                "lore",
                encoding="utf-8",
            )
            result = backup.create_local_snapshot(
                root,
                release_id="release-resume",
                policy=self._policy(root),
            )
            store = root / ".silicon" / "snapshots"
            entries = result.manifest["files"]
            self.assertGreaterEqual(len(entries), 2)
            first = entries[0]
            first_path = root / str(first["path"])
            first_path.parent.mkdir(parents=True, exist_ok=True)
            first_path.write_bytes(
                (
                    store
                    / "objects"
                    / "sha256"
                    / str(first["sha256"])[:2]
                    / str(first["sha256"])[2:]
                ).read_bytes()
            )
            os.chmod(first_path, int(first["mode"]))
            for entry in entries[1:]:
                target = root / str(entry["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("stale", encoding="utf-8")
            (root / backup.IN_PLACE_RESTORE_JOURNAL).write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "root_hash": result.root_hash,
                        "release_id": "release-resume",
                        "state": "APPLYING",
                        "operation_count": len(entries),
                        "next_operation": 1,
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    }
                ),
                encoding="utf-8",
            )

            backup.restore_local_snapshot_in_place(
                root,
                result.root_hash,
                store=store,
            )

            journal = json.loads(
                (root / backup.IN_PLACE_RESTORE_JOURNAL).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(journal["state"], "COMMITTED")
            self.assertEqual(journal["verified_root_hash"], result.root_hash)
            for entry in entries:
                self.assertEqual(
                    hashlib.sha256(
                        (root / str(entry["path"])).read_bytes()
                    ).hexdigest(),
                    entry["sha256"],
                )

    def test_in_place_restore_rejects_symlinked_destination_parent(self):
        with (
            tempfile.TemporaryDirectory() as td,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = self._root_with_memory(td)
            result = backup.create_local_snapshot(
                root,
                release_id="release-1",
                policy=self._policy(root),
            )
            store = root / ".silicon" / "snapshots"
            (root / "prompts" / "MEMORY.md").unlink()
            (root / "prompts").rmdir()
            try:
                (root / "prompts").symlink_to(
                    Path(outside),
                    target_is_directory=True,
                )
            except OSError:
                self.skipTest("symlinks are not available")

            with self.assertRaises(data_policy.UnsafePathError):
                backup.restore_local_snapshot_in_place(
                    root,
                    result.root_hash,
                    store=store,
                )
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_in_place_restore_rejects_an_external_snapshot_store(self):
        with (
            tempfile.TemporaryDirectory() as td,
            tempfile.TemporaryDirectory() as external,
        ):
            root = self._root_with_memory(td)
            result = backup.create_local_snapshot(
                root,
                release_id="release-1",
                policy=self._policy(root),
                store=Path(external),
            )
            (root / ".silicon" / "snapshots").mkdir(parents=True)

            with self.assertRaises(data_policy.UnsafePathError):
                backup.restore_local_snapshot_in_place(
                    root,
                    result.root_hash,
                    store=Path(external),
                )

    def test_in_place_restore_cannot_replace_generation_pointer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".silicon").mkdir()
            (root / ".silicon" / "current.json").write_text(
                '{"generation_id":"trusted"}\n',
                encoding="utf-8",
            )
            policy = data_policy.DataPolicy(
                classes={},
                additive={
                    "legacy_additive": (".silicon/current.json",),
                },
            )
            result = backup.create_local_snapshot(
                root,
                release_id="release-1",
                policy=policy,
            )

            with self.assertRaises(backup.SnapshotIntegrityError):
                backup.restore_local_snapshot_in_place(
                    root,
                    result.root_hash,
                    store=root / ".silicon" / "snapshots",
                )

    def test_manifest_traversal_is_rejected_before_restore(self):
        digest = hashlib.sha256(b"x").hexdigest()
        body = {
            "schema": backup.SNAPSHOT_SCHEMA,
            "release_id": "release",
            "files": [
                {
                    "path": "../escape",
                    "sha256": digest,
                    "size": 1,
                    "mode": 0o600,
                    "classes": ["critical_living"],
                }
            ],
            "tombstones": [],
        }
        body["root_hash"] = hashlib.sha256(
            json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(backup.SnapshotIntegrityError):
                backup.restore_snapshot(
                    body,
                    Path(td) / "restore",
                    store=Path(td) / "store",
                    dry_run=True,
                )


class StreamingArchiveTest(unittest.TestCase):
    def test_archive_spills_to_disk_and_contains_only_selected_regular_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            payload = os.urandom(32_000)
            (root / "data" / "payload.bin").write_bytes(payload)
            (root / "outside.txt").write_text("outside", encoding="utf-8")

            archive, included = backup.build_archive_file(
                root,
                ["data/**"],
                spool_limit=128,
            )
            try:
                self.assertTrue(getattr(archive, "_rolled"))
                with tarfile.open(fileobj=archive, mode="r:gz") as reader:
                    names = set(reader.getnames())
                    restored = reader.extractfile("data/payload.bin")
                    self.assertIsNotNone(restored)
                    self.assertEqual(restored.read(), payload)
            finally:
                archive.close()

            self.assertEqual(included, ["data"])
            self.assertNotIn("outside.txt", names)

    def test_archive_rejects_traversal_before_opening_any_source(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(data_policy.DataPolicyError):
                backup.build_archive_file(Path(td), ["../outside"])


if __name__ == "__main__":
    unittest.main()
