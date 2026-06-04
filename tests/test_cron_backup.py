import io
import json
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import core.backup as backup
import core.cron as cron


class FakeCronClient:
    def __init__(self, records):
        self.records = records

    def crons_list(self):
        return {"crons": self.records}


class CronMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = cron.CRON_STATE_FILE
        cron.CRON_STATE_FILE = Path(self.tmp.name) / "crons.json"

    def tearDown(self):
        cron.CRON_STATE_FILE = self.old_state
        self.tmp.cleanup()

    def record(self, active=True):
        return {
            "cron_id": "cron-1",
            "trigger": "*/5 * * * *",
            "timezone": "UTC",
            "task": "check something",
            "active": active,
            "for_targets": [{"kind": "carbon", "id": "carbon-a"}],
        }

    def test_first_seen_cron_sets_watermark_without_backfill(self):
        now = datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)
        with mock.patch.object(cron, "ensure_contact_for_target") as ensure:
            result = cron._check_glass_crons(now=now, client=FakeCronClient([self.record()]))

        self.assertEqual(result, {})
        ensure.assert_not_called()
        state = json.loads(cron.CRON_STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(state["crons"]["cron-1"]["watermark_utc"], now.isoformat())

    def test_one_due_run_triggers_one_context(self):
        watermark = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
        cron.CRON_STATE_FILE.write_text(json.dumps({"version": 1, "crons": {"cron-1": {"watermark_utc": watermark.isoformat()}}}))

        contact = {"contact_type": "carbon", "carbon_id": "carbon-a"}
        with mock.patch.object(cron, "ensure_contact_for_target", return_value=contact):
            result = cron._check_glass_crons(now=now, client=FakeCronClient([self.record()]))

        self.assertIn("carbon-a", result)
        self.assertIn("scheduled_fire_time_utc: 2026-01-01T00:05:00+00:00", result["carbon-a"])
        self.assertNotIn("missed_run_count", result["carbon-a"])

    def test_many_missed_runs_collapse(self):
        watermark = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
        cron.CRON_STATE_FILE.write_text(json.dumps({"version": 1, "crons": {"cron-1": {"watermark_utc": watermark.isoformat()}}}))

        contact = {"contact_type": "carbon", "carbon_id": "carbon-a"}
        with mock.patch.object(cron, "ensure_contact_for_target", return_value=contact):
            result = cron._check_glass_crons(now=now, client=FakeCronClient([self.record()]))

        self.assertIn("missed_run_count: 4", result["carbon-a"])
        self.assertIn("collapsed: true", result["carbon-a"])
        self.assertIn("scheduled_fire_time_utc: 2026-01-01T00:20:00+00:00", result["carbon-a"])

    def test_inactive_crons_do_not_run(self):
        watermark = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        now = datetime(2026, 1, 1, 0, 20, tzinfo=timezone.utc)
        cron.CRON_STATE_FILE.write_text(json.dumps({"version": 1, "crons": {"cron-1": {"watermark_utc": watermark.isoformat()}}}))

        with mock.patch.object(cron, "ensure_contact_for_target") as ensure:
            result = cron._check_glass_crons(now=now, client=FakeCronClient([self.record(active=False)]))

        self.assertEqual(result, {})
        ensure.assert_not_called()


class BackupManifestTest(unittest.TestCase):
    def test_manifest_archive_includes_matching_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".backupsilicon").write_text("prompts/MEMORY.md\nprompts/memory/**\n", encoding="utf-8")
            (root / "prompts" / "memory" / "carbons").mkdir(parents=True)
            (root / "prompts" / "MEMORY.md").write_text("memory", encoding="utf-8")
            (root / "prompts" / "memory" / "carbons" / "a.md").write_text("a", encoding="utf-8")
            (root / "other.txt").write_text("skip", encoding="utf-8")

            patterns = backup.read_manifest(root)
            data, included = backup.build_archive(root, patterns)
            names = set(included)

            self.assertIn("prompts/MEMORY.md", names)
            self.assertIn("prompts/memory", names)

            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                tar_names = set(tar.getnames())
            self.assertIn("prompts/MEMORY.md", tar_names)
            self.assertIn("prompts/memory/carbons/a.md", tar_names)
            self.assertNotIn("other.txt", tar_names)


if __name__ == "__main__":
    unittest.main()
