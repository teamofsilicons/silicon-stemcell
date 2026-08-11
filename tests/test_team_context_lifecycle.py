import threading
import time
import unittest
from unittest import mock

import config
import main


class TeamContextLifecycleTest(unittest.TestCase):
    def setUp(self):
        with config._TEAM_CONTEXT_LOCK:
            config._TEAM_CONTEXT_RUNNING = False
            config._TEAM_CONTEXT_PENDING_NOTICE = ""
            config._TEAM_CONTEXT_LAST_NOTICE = ""
            config._TEAM_CONTEXT_MAINTENANCE_ACTIVITY = None
            config._TEAM_CONTEXT_RESULT_EPOCH = 0
            config._TEAM_CONTEXT_NEXT_SAFETY_CHECK = 0.0
            config._TEAM_CONTEXT_OWN_SIGNATURE = object()

    def test_startup_sync_precedes_restart_manager_turn(self):
        calls = []
        dispatcher = mock.Mock()
        dispatcher.submit.side_effect = lambda context: calls.append(
            ("manager", context)
        )

        with (
            mock.patch.object(main, "_install_diagnostic_shutdown_hooks"),
            mock.patch.object(main, "start_listener"),
            mock.patch.object(main, "stop_listener"),
            mock.patch.object(main, "complete_inactive_calls"),
            mock.patch.object(main, "ManagerDispatcher", return_value=dispatcher),
            mock.patch.object(
                main,
                "_bootstrap_team_context",
                side_effect=lambda: calls.append("team-context"),
            ),
            mock.patch.object(
                main,
                "_bootstrap_trust_policy",
                side_effect=lambda: calls.append("trust-policy"),
            ),
            mock.patch.object(
                main,
                "_check_restart_flag",
                side_effect=lambda: (
                    calls.append("restart-check") or ("restarted", "carbon-a")
                ),
            ),
            mock.patch.object(
                main,
                "validate_contacts_integrity",
                side_effect=KeyboardInterrupt,
            ),
            self.assertRaises(SystemExit),
        ):
            main.main()

        self.assertEqual(
            calls[:4],
            [
                "team-context",
                "trust-policy",
                "restart-check",
                ("manager", {"carbon-a": "restarted"}),
            ],
        )
        dispatcher.shutdown.assert_called_once_with(wait=False)

    def test_recovery_loop_runs_team_context_tick(self):
        self.assertEqual(config.LOOP_TICK, 60)
        self.assertEqual(config.EVENT_LOOP[0]["name"], "check_team_context")
        finished = threading.Event()

        with mock.patch(
            "interface.team_context.team_context_tick",
            side_effect=lambda: (
                finished.set()
                or {
                    "ok": True,
                    "status": "current",
                    "own_status": "unchanged",
                }
            ),
        ) as tick:
            self.assertIsNone(config.check_team_context())
            self.assertTrue(finished.wait(2))

        tick.assert_called_once_with()

    def test_manager_dispatcher_serializes_one_contact_without_blocking_another(self):
        first_started = threading.Event()
        release_first = threading.Event()
        other_finished = threading.Event()
        calls = []
        lock = threading.Lock()

        def runner(context):
            contact_id = next(iter(context))
            with lock:
                calls.append((contact_id, context[contact_id]))
            if contact_id == "carbon-a" and not first_started.is_set():
                first_started.set()
                release_first.wait(2)
            if contact_id == "carbon-b":
                other_finished.set()

        dispatcher = main.ManagerDispatcher(runner=runner)
        dispatcher.submit({"carbon-a": "first"})
        self.assertTrue(first_started.wait(2))
        dispatcher.submit({"carbon-a": "second", "carbon-b": "independent"})

        self.assertTrue(other_finished.wait(2))
        release_first.set()
        self.assertTrue(dispatcher.wait_for_idle(2))

        carbon_a = [body for contact, body in calls if contact == "carbon-a"]
        self.assertEqual(carbon_a, ["first", "second"])
        self.assertIn(("carbon-b", "independent"), calls)

    def test_tick_is_nonblocking_and_coalesces_while_running(self):
        started = threading.Event()
        release = threading.Event()

        def slow_tick():
            started.set()
            release.wait(2)
            return {"ok": True, "status": "current"}

        with mock.patch(
            "interface.team_context.team_context_tick",
            side_effect=slow_tick,
        ) as tick:
            before = time.monotonic()
            self.assertIsNone(config.check_team_context())
            self.assertLess(time.monotonic() - before, 0.25)
            self.assertTrue(started.wait(2))
            self.assertIsNone(config.check_team_context())
            self.assertEqual(tick.call_count, 1)
            release.set()

        deadline = time.monotonic() + 2
        while config._TEAM_CONTEXT_RUNNING and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(config._TEAM_CONTEXT_RUNNING)

    def test_conflict_notice_is_delivered_once_to_central_manager(self):
        finished = threading.Event()

        def conflict_tick():
            finished.set()
            return {
                "ok": False,
                "status": "conflict",
                "local_saved": True,
                "actual_revision": 7,
            }

        with (
            mock.patch(
                "interface.team_context.team_context_tick",
                side_effect=conflict_tick,
            ),
            mock.patch(
                "interface.get_central_contact_id",
                return_value="central-carbon",
            ),
        ):
            self.assertIsNone(config.check_team_context())
            self.assertTrue(finished.wait(2))
            deadline = time.monotonic() + 2
            while config._TEAM_CONTEXT_RUNNING and time.monotonic() < deadline:
                time.sleep(0.01)
            notice = config.check_team_context()

        self.assertEqual(list(notice), ["central-carbon"])
        self.assertIn("conflicts with a newer Glass revision", notice["central-carbon"])

    def test_authorization_loss_notice_explains_fail_closed_cleanup(self):
        notice = config._team_context_notice({"ok": False, "status": "unauthorized"})

        self.assertIn("no longer authorizes", notice)
        self.assertIn("hidden the cached TEAM.md", notice)

    def test_invalid_notice_preserves_the_sync_detail(self):
        notice = config._team_context_notice(
            {
                "ok": False,
                "status": "partial",
                "own_status": "invalid",
                "own_detail": (
                    "Local context file changed while it was being read."
                ),
            }
        )

        self.assertIn(
            "Local context file changed while it was being read.",
            notice,
        )
        self.assertIn("stable regular file", notice)

    def test_healthy_tick_discards_a_superseded_pending_notice(self):
        invalid = {
            "ok": False,
            "status": "invalid",
            "detail": "Local context file changed while it was being read.",
        }
        healthy = {
            "ok": True,
            "status": "current",
            "own_status": "unchanged",
        }

        with (
            mock.patch(
                "interface.team_context.team_context_tick",
                side_effect=[invalid, healthy],
            ),
            mock.patch("builtins.print"),
        ):
            config._run_team_context_tick()
            config._run_team_context_tick()

        with config._TEAM_CONTEXT_LOCK:
            self.assertEqual(config._TEAM_CONTEXT_PENDING_NOTICE, "")
            self.assertEqual(config._TEAM_CONTEXT_LAST_NOTICE, "")

    def test_concurrent_recovery_cancels_a_notice_before_delivery(self):
        notice = config._team_context_notice(
            {
                "ok": False,
                "status": "invalid",
                "detail": "Local context path must be a regular file.",
            }
        )
        with config._TEAM_CONTEXT_LOCK:
            config._TEAM_CONTEXT_PENDING_NOTICE = notice
            config._TEAM_CONTEXT_LAST_NOTICE = notice
            config._TEAM_CONTEXT_RUNNING = True

        def recover_before_contact_lookup_finishes():
            config.acknowledge_team_context_result(
                {
                    "ok": True,
                    "status": "uploaded",
                    "revision": 2,
                }
            )
            return "central-carbon"

        with mock.patch(
            "interface.get_central_contact_id",
            side_effect=recover_before_contact_lookup_finishes,
        ):
            delivered = config.check_team_context()

        self.assertIsNone(delivered)
        with config._TEAM_CONTEXT_LOCK:
            self.assertEqual(config._TEAM_CONTEXT_PENDING_NOTICE, "")
            self.assertEqual(config._TEAM_CONTEXT_LAST_NOTICE, "")
            config._TEAM_CONTEXT_RUNNING = False

    def test_successful_update_supersedes_an_inflight_invalid_result(self):
        started = threading.Event()
        release = threading.Event()

        def stale_invalid_tick():
            started.set()
            release.wait(2)
            return {
                "ok": False,
                "status": "invalid",
                "detail": "Local context file changed while it was being read.",
            }

        with (
            mock.patch(
                "interface.team_context.team_context_tick",
                side_effect=stale_invalid_tick,
            ),
            mock.patch("builtins.print") as output,
        ):
            thread = threading.Thread(target=config._run_team_context_tick)
            thread.start()
            self.assertTrue(started.wait(2))
            config.acknowledge_team_context_result(
                {
                    "ok": True,
                    "status": "uploaded",
                    "revision": 2,
                }
            )
            release.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        output.assert_not_called()
        with config._TEAM_CONTEXT_LOCK:
            self.assertEqual(config._TEAM_CONTEXT_PENDING_NOTICE, "")
            self.assertEqual(config._TEAM_CONTEXT_LAST_NOTICE, "")

    def test_transient_failure_does_not_reset_notice_deduplication(self):
        invalid = {
            "ok": False,
            "status": "invalid",
            "detail": "Local context path must be a regular file.",
        }
        unavailable = {"ok": False, "status": "unavailable"}

        with (
            mock.patch(
                "interface.team_context.team_context_tick",
                side_effect=[invalid, unavailable, invalid],
            ),
            mock.patch("builtins.print") as output,
        ):
            config._run_team_context_tick()
            with config._TEAM_CONTEXT_LOCK:
                config._TEAM_CONTEXT_PENDING_NOTICE = ""
            config._run_team_context_tick()
            config._run_team_context_tick()

        self.assertEqual(output.call_count, 1)
        with config._TEAM_CONTEXT_LOCK:
            self.assertEqual(config._TEAM_CONTEXT_PENDING_NOTICE, "")
            self.assertIn(
                "regular file",
                config._TEAM_CONTEXT_LAST_NOTICE,
            )

    def test_notice_waits_until_a_central_contact_exists(self):
        with config._TEAM_CONTEXT_LOCK:
            config._TEAM_CONTEXT_PENDING_NOTICE = "Action is required."
            config._TEAM_CONTEXT_RUNNING = True

        with mock.patch(
            "interface.get_central_contact_id",
            side_effect=["", "central-carbon"],
        ):
            self.assertIsNone(config.check_team_context())
            with config._TEAM_CONTEXT_LOCK:
                self.assertEqual(
                    config._TEAM_CONTEXT_PENDING_NOTICE,
                    "Action is required.",
                )
            delivered = config.check_team_context()

        self.assertEqual(
            delivered,
            {
                "central-carbon": (
                    "Team context synchronization notice:\nAction is required."
                )
            },
        )
        with config._TEAM_CONTEXT_LOCK:
            self.assertEqual(config._TEAM_CONTEXT_PENDING_NOTICE, "")
            config._TEAM_CONTEXT_RUNNING = False


if __name__ == "__main__":
    unittest.main()
