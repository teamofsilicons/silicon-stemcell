from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from manager.runtime import health as runtime_health


class RuntimeHealthTests(unittest.TestCase):
    def tearDown(self):
        runtime_health.stop_runtime_health()

    def test_publishes_ready_identity_and_removes_only_own_record(self):
        with tempfile.TemporaryDirectory() as raw:
            health = Path(raw) / "runtime-health.json"
            retry_summary = {
                "pending": 2,
                "failed": 1,
                "dead_letter": 3,
                "total": 5,
                "archived_dead_letter": 4,
                "overflow_count": 6,
                "last_overflow_at": 7.0,
                "oldest_created_at": 8.0,
                "next_attempt_at": 9.0,
                "message": "must never reach runtime health",
            }
            with (
                mock.patch.object(runtime_health, "HEALTH_FILE", health),
                mock.patch(
                    "interface.work.pending_call_update_retries",
                    return_value=retry_summary,
                ) as retry_health,
                mock.patch(
                    "interface.messages.manager_queue_health",
                    return_value={
                        "queued": 10,
                        "capacity": 1000,
                        "overflow_count": 2,
                        "last_overflow_at": 11.0,
                        "message": "must never reach runtime health",
                    },
                ) as queue_health,
            ):
                value = runtime_health.publish_runtime_health(
                    lambda: "updating"
                )
                self.assertTrue(value["ready"])
                self.assertEqual(value["phase"], "updating")
                self.assertEqual(value["pid"], runtime_health.os.getpid())
                self.assertEqual(
                    value["call_retry"],
                    {
                        "available": True,
                        "pending": 2,
                        "failed": 1,
                        "dead_letter": 3,
                        "total": 5,
                        "archived_dead_letter": 4,
                        "overflow_count": 6,
                        "last_overflow_at": 7.0,
                        "oldest_created_at": 8.0,
                        "next_attempt_at": 9.0,
                    },
                )
                retry_health.assert_called_once_with(persist_prune=False)
                self.assertEqual(
                    value["manager_queue"],
                    {
                        "available": True,
                        "queued": 10,
                        "capacity": 1000,
                        "overflow_count": 2,
                        "last_overflow_at": 11.0,
                    },
                )
                queue_health.assert_called_once_with()
                self.assertNotIn("message", value["call_retry"])
                self.assertNotIn("message", value["manager_queue"])
                self.assertTrue(health.is_file())

                runtime_health.stop_runtime_health()
                self.assertFalse(health.exists())

    def test_retry_health_failure_is_body_free_and_does_not_break_readiness(self):
        with tempfile.TemporaryDirectory() as raw:
            health = Path(raw) / "runtime-health.json"
            with (
                mock.patch.object(runtime_health, "HEALTH_FILE", health),
                mock.patch(
                    "interface.work.pending_call_update_retries",
                    side_effect=RuntimeError("TOP-SECRET TRANSCRIPT"),
                ),
                mock.patch(
                    "interface.messages.manager_queue_health",
                    side_effect=RuntimeError("OTHER PRIVATE BODY"),
                ),
            ):
                value = runtime_health.publish_runtime_health()

            self.assertTrue(value["ready"])
            self.assertEqual(value["call_retry"], {"available": False})
            self.assertEqual(value["manager_queue"], {"available": False})
            self.assertNotIn("TOP-SECRET", health.read_text(encoding="utf-8"))
            self.assertNotIn("OTHER PRIVATE", health.read_text(encoding="utf-8"))

    def test_background_heartbeat_advances(self):
        with tempfile.TemporaryDirectory() as raw:
            health = Path(raw) / "runtime-health.json"
            with (
                mock.patch.object(runtime_health, "HEALTH_FILE", health),
                mock.patch(
                    "interface.work.pending_call_update_retries",
                    return_value={},
                ),
                mock.patch(
                    "interface.messages.manager_queue_health",
                    return_value={},
                ),
            ):
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
