from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import runtime_health


class RuntimeHealthTests(unittest.TestCase):
    def tearDown(self):
        runtime_health.stop_runtime_health()

    def test_publishes_ready_identity_and_removes_only_own_record(self):
        with tempfile.TemporaryDirectory() as raw:
            health = Path(raw) / "runtime-health.json"
            with mock.patch.object(runtime_health, "HEALTH_FILE", health):
                value = runtime_health.publish_runtime_health(
                    lambda: "updating"
                )
                self.assertTrue(value["ready"])
                self.assertEqual(value["phase"], "updating")
                self.assertEqual(value["pid"], runtime_health.os.getpid())
                self.assertTrue(health.is_file())

                runtime_health.stop_runtime_health()
                self.assertFalse(health.exists())

    def test_background_heartbeat_advances(self):
        with tempfile.TemporaryDirectory() as raw:
            health = Path(raw) / "runtime-health.json"
            with mock.patch.object(runtime_health, "HEALTH_FILE", health):
                runtime_health.start_runtime_health(
                    lambda: "available",
                    heartbeat_seconds=0.05,
                )
                first = runtime_health.read_json(health, {})["heartbeat_at"]
                deadline = time.monotonic() + 1
                latest = first
                while latest <= first and time.monotonic() < deadline:
                    time.sleep(0.02)
                    latest = runtime_health.read_json(health, {})[
                        "heartbeat_at"
                    ]
                self.assertGreater(latest, first)


if __name__ == "__main__":
    unittest.main()
