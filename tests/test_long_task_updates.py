import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import main
from core import long_task_updates, work_updates


DONE = "Done. work_update accepted"
ERROR = "Error: work_update failed"


class LongTaskLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_state_file = long_task_updates.LONG_TASK_STATE_FILE
        long_task_updates.LONG_TASK_STATE_FILE = (
            Path(self.temp.name) / "long_task_updates.json"
        )
        long_task_updates.reset_long_task_registry_for_tests()
        self.refresh_patch = mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={},
        )
        self.refresh_patch.start()

    def tearDown(self):
        self.refresh_patch.stop()
        long_task_updates.reset_long_task_registry_for_tests()
        long_task_updates.LONG_TASK_STATE_FILE = self.old_state_file

    def lifecycle(self):
        return long_task_updates.LongTaskLifecycle(
            "carbon-a",
            "run-a",
            "room_id: room-a\nevent_id: event-a\nmessage:\nBuild the release",
            auto_start=False,
        )

    def accept_task(
        self,
        lifecycle,
        *,
        task_id="manager-task",
        realistic_estimate_seconds=None,
        estimate_seconds=None,
        todos=None,
    ):
        data = {
            "task_id": task_id,
            "title": "Build the release",
        }
        if realistic_estimate_seconds is not None:
            data["realistic_estimate_seconds"] = realistic_estimate_seconds
        if estimate_seconds is not None:
            data["estimate_seconds"] = estimate_seconds
        if todos is not None:
            data["todos"] = todos
        spec = {
            "tool": "work_update",
            "action": "task/create",
            "data": data,
        }
        prepared = lifecycle.prepare_work_update(spec)
        lifecycle.record_work_update(spec, prepared, [DONE])
        return spec, prepared

    def register_lifecycle(self, lifecycle):
        with long_task_updates._REGISTRY_LOCK:
            long_task_updates._ACTIVE_BY_CONTACT[
                lifecycle.contact_id
            ] = lifecycle

    def expire_lease(self, contact_id="carbon-a"):
        def mutate(state):
            entry = state["contacts"][contact_id]
            entry["lease_until"] = 0
            entry["lease_pid"] = 0

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            mutate,
        )

    def test_success_detection_uses_transport_status_not_returned_content(self):
        self.assertTrue(
            long_task_updates._successful(
                'Done. work_update task/update: {"description":"Error: fixed"}'
            )
        )

    def test_elapsed_time_never_creates_a_task_or_todo(self):
        lifecycle = self.lifecycle()
        lifecycle.started_at = time.time() - 24 * 60 * 60
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            return_value=DONE,
        ) as execute:
            self.assertEqual(lifecycle.ensure("working"), "")
            lifecycle.replay_pending_once()

        execute.assert_not_called()
        self.assertEqual(lifecycle.task_id, "")
        self.assertEqual(lifecycle.todo_id, "")

    def test_failed_create_backs_off_instead_of_retrying_each_watch_tick(self):
        lifecycle = self.lifecycle()
        spec = {
            "tool": "work_update",
            "action": "task/create",
            "data": {"task_id": "manager-task", "title": "Manager task"},
        }
        prepared = lifecycle.prepare_work_update(spec)
        lifecycle.record_work_update(spec, prepared, [ERROR])
        lifecycle._next_create_attempt_at = 0
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            return_value=ERROR,
        ) as execute:
            self.assertEqual(lifecycle.ensure("working"), "")
            self.assertEqual(lifecycle.ensure("working"), "")

        execute.assert_called_once()
        self.assertGreater(lifecycle._next_create_attempt_at, 0)

    def test_create_intent_survives_restart_with_identical_ids(self):
        first = self.lifecycle()
        spec = {
            "tool": "work_update",
            "action": "task/create",
            "data": {"task_id": "manager-task", "title": "Manager task"},
        }
        prepared = first.prepare_work_update(spec)
        first.record_work_update(spec, prepared, [ERROR])
        first._next_create_attempt_at = 0
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            side_effect=[ERROR, DONE],
        ) as execute:
            self.assertEqual(first.ensure("working"), "")
            intended_task_id = first.task_id
            intended_client_id = execute.call_args_list[0].args[0]["data"][
                "client_id"
            ]
            saved = long_task_updates._state_entry("carbon-a")
            first._closed = True
            first._stop.set()
            self.expire_lease()

            restarted = long_task_updates.LongTaskLifecycle(
                "carbon-a",
                "different-random-run",
                "message:\nBuild the release",
                saved=saved,
                auto_start=False,
            )
            restarted._next_create_attempt_at = 0
            self.assertEqual(restarted.ensure("working"), intended_task_id)

        retry_payload = execute.call_args_list[1].args[0]["data"]
        self.assertEqual(restarted.run_id, "run-a")
        self.assertEqual(retry_payload["task_id"], intended_task_id)
        self.assertEqual(retry_payload["client_id"], intended_client_id)

    def test_model_create_inflight_suppresses_watchdog_without_timeout(self):
        lifecycle = self.lifecycle()
        model_create = {
            "tool": "work_update",
            "action": "task/create",
            "data": {"task_id": "model-task", "title": "Model task"},
        }
        lifecycle.prepare_work_update(model_create)
        lifecycle._model_create_started_at = time.time() - 10_000
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
        ) as execute:
            self.assertEqual(lifecycle.ensure("working"), "")
        execute.assert_not_called()

    def test_missing_task_does_not_block_or_fabricate_a_final_card(self):
        lifecycle = self.lifecycle()
        lifecycle.started_at = time.time() - 24 * 60 * 60
        sender = mock.Mock(return_value="Message sent")
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
        ) as execute:
            status = lifecycle.deliver_final_reply(
                "All done.",
                has_active_workers=False,
                reply_sender=sender,
            )

        self.assertEqual(status, "Message sent")
        sender.assert_called_once()
        execute.assert_not_called()
        saved = long_task_updates._state_entry("carbon-a")
        self.assertFalse(saved.get("active"))

    def test_manager_created_task_is_adopted(self):
        lifecycle = self.lifecycle()
        model_create = {
            "tool": "work_update",
            "action": "task/create",
            "data": {
                "task_id": "release-task",
                "title": "Ship the release",
            },
        }
        prepared = lifecycle.prepare_work_update(model_create)
        self.assertEqual(prepared[0]["data"]["task_id"], "release-task")
        self.assertTrue(prepared[0]["data"]["client_id"])

        with (
            mock.patch.object(
                long_task_updates,
                "active_task_id",
                return_value="release-task",
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
            ) as execute,
        ):
            lifecycle.record_work_update(model_create, prepared, [DONE])
            self.assertEqual(lifecycle.ensure("working"), "release-task")

        execute.assert_not_called()

    def test_accepted_realistic_estimate_schedules_exact_five_percent_reviews(
        self,
    ):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )

        schedule = lifecycle.accuracy_schedule
        self.assertEqual(schedule["task_id"], "manager-task")
        self.assertEqual(schedule["goal_seconds"], 105)
        self.assertEqual(schedule["interval_seconds"], 5.25)
        self.assertEqual(schedule["next_checkpoint"], 1)
        self.assertEqual(schedule["pending_review"], {})
        self.assertEqual(
            long_task_updates._goal_seconds_from_data(
                {
                    "estimate_seconds": 120,
                    "realistic_estimate_seconds": 100,
                }
            ),
            120,
        )

    def test_task_without_estimate_has_no_accuracy_schedule(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)

        self.assertEqual(lifecycle.accuracy_schedule, {})

    def test_accuracy_review_coalesces_missed_checkpoints_and_continues_past_goal(
        self,
    ):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = 1_000.0
        lifecycle._persist(active=True)
        snapshot = {
            "task_id": "manager-task",
            "state": "running",
            "estimate_seconds": 105,
            "todos": [],
        }

        with (
            mock.patch.object(
                long_task_updates.time,
                "time",
                return_value=1_000.0 + 13 * 5.25,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value=snapshot,
            ),
        ):
            self.assertTrue(lifecycle._prepare_accuracy_review_if_due())
            self.assertFalse(lifecycle._prepare_accuracy_review_if_due())

        pending = lifecycle.accuracy_schedule["pending_review"]
        first_review_id = pending["review_id"]
        self.assertEqual(pending["checkpoint_from"], 1)
        self.assertEqual(pending["checkpoint_through"], 13)
        self.assertLessEqual(
            len(pending["context"]),
            long_task_updates.MAX_ACCURACY_REVIEW_CONTEXT_CHARS,
        )
        self.assertTrue(lifecycle.complete_accuracy_review(first_review_id))
        self.assertEqual(
            lifecycle.accuracy_schedule["next_checkpoint"],
            14,
        )

        with (
            mock.patch.object(
                long_task_updates.time,
                "time",
                return_value=1_000.0 + 21 * 5.25,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value=snapshot,
            ),
        ):
            self.assertTrue(lifecycle._prepare_accuracy_review_if_due())

        overrun = lifecycle.accuracy_schedule["pending_review"]
        self.assertEqual(overrun["checkpoint_from"], 14)
        self.assertEqual(overrun["checkpoint_through"], 21)

    def test_material_estimate_change_resets_accuracy_schedule(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        previous_generation = lifecycle.accuracy_schedule["generation"]
        update = {
            "tool": "work_update",
            "action": "task/update",
            "task_id": "manager-task",
            "data": {"realistic_estimate_seconds": 200},
        }
        with mock.patch.object(
            long_task_updates.time,
            "time",
            return_value=2_000.0,
        ):
            lifecycle.record_work_update(update, [update], [DONE])

        schedule = lifecycle.accuracy_schedule
        self.assertEqual(schedule["goal_seconds"], 210)
        self.assertEqual(schedule["interval_seconds"], 10.5)
        self.assertEqual(schedule["anchor_at"], 2_000.0)
        self.assertEqual(schedule["next_checkpoint"], 1)
        self.assertNotEqual(schedule["generation"], previous_generation)

    def test_estimate_change_uses_material_one_percent_boundary(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            estimate_seconds=1_000,
        )
        original_generation = lifecycle.accuracy_schedule["generation"]
        below_boundary = {
            "tool": "work_update",
            "action": "task/update",
            "task_id": "manager-task",
            "data": {"estimate_seconds": 1_009},
        }
        lifecycle.record_work_update(
            below_boundary,
            [below_boundary],
            [DONE],
        )

        self.assertEqual(
            lifecycle.accuracy_schedule["generation"],
            original_generation,
        )
        self.assertEqual(
            lifecycle.accuracy_schedule["goal_seconds"],
            1_000,
        )

        at_boundary = {
            "tool": "work_update",
            "action": "task/update",
            "task_id": "manager-task",
            "data": {"estimate_seconds": 1_010},
        }
        lifecycle.record_work_update(
            at_boundary,
            [at_boundary],
            [DONE],
        )
        self.assertNotEqual(
            lifecycle.accuracy_schedule["generation"],
            original_generation,
        )
        self.assertEqual(
            lifecycle.accuracy_schedule["goal_seconds"],
            1_010,
        )

    def test_manager_estimate_clear_cancels_but_omission_preserves_schedule(
        self,
    ):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            estimate_seconds=1_000,
        )
        generation = lifecycle.accuracy_schedule["generation"]
        omitted = {
            "tool": "work_update",
            "action": "task/update",
            "task_id": "manager-task",
            "data": {"description": "Scope is unchanged."},
        }
        lifecycle.record_work_update(omitted, [omitted], [DONE])
        self.assertEqual(
            lifecycle.accuracy_schedule["generation"],
            generation,
        )

        cleared = {
            "tool": "work_update",
            "action": "task/update",
            "task_id": "manager-task",
            "data": {"estimate_seconds": 0},
        }
        lifecycle.record_work_update(cleared, [cleared], [DONE])
        self.assertEqual(lifecycle.accuracy_schedule, {})

    def test_refreshed_estimate_clear_cancels_but_omission_does_not(self):
        anchor = time.time()
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = anchor
        with (
            mock.patch.object(
                long_task_updates.time,
                "time",
                return_value=anchor + 6,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "manager-task",
                    "state": "running",
                },
            ),
        ):
            self.assertTrue(lifecycle._prepare_accuracy_review_if_due())
        self.assertTrue(lifecycle.accuracy_schedule)

        cleared_lifecycle = long_task_updates.LongTaskLifecycle(
            "carbon-b",
            "run-b",
            "message:\nBuild another release",
            auto_start=False,
        )
        self.accept_task(
            cleared_lifecycle,
            realistic_estimate_seconds=100,
        )
        cleared_lifecycle.accuracy_schedule["anchor_at"] = anchor
        with (
            mock.patch.object(
                long_task_updates.time,
                "time",
                return_value=anchor + 6,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "manager-task",
                    "state": "running",
                    "estimate_seconds": 0,
                },
            ),
        ):
            self.assertFalse(
                cleared_lifecycle._prepare_accuracy_review_if_due()
            )
        self.assertEqual(cleared_lifecycle.accuracy_schedule, {})

    def test_terminal_task_cancels_accuracy_schedule_and_pending_review(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = 1_000.0
        with (
            mock.patch.object(
                long_task_updates.time,
                "time",
                return_value=1_006.0,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "manager-task",
                    "state": "running",
                    "estimate_seconds": 105,
                },
            ),
        ):
            self.assertTrue(lifecycle._prepare_accuracy_review_if_due())

        terminal = {
            "tool": "work_update",
            "action": "task/complete",
            "task_id": "manager-task",
            "data": {"body": "Completed."},
        }
        lifecycle.record_work_update(terminal, [terminal], [DONE])
        self.register_lifecycle(lifecycle)

        self.assertEqual(lifecycle.accuracy_schedule, {})
        self.assertEqual(
            long_task_updates.claim_ready_accuracy_review_roots(),
            {},
        )

    def test_remote_terminal_accuracy_refresh_closes_and_unregisters(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = time.time() - 6
        self.register_lifecycle(lifecycle)
        with mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={
                "task_id": "manager-task",
                "state": "completed",
                "estimate_seconds": 105,
            },
        ):
            self.assertFalse(lifecycle._prepare_accuracy_review_if_due())

        self.assertFalse(lifecycle.is_open)
        self.assertIsNone(long_task_updates.current_long_task("carbon-a"))
        self.assertFalse(
            long_task_updates._state_entry("carbon-a").get("active")
        )
        self.assertFalse(
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "run-next",
                "event_id: next\nmessage:\nNext request",
                visible=True,
            )
        )

    def test_internal_terminal_accuracy_update_closes_and_unregisters(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = time.time() - 6
        with mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={
                "task_id": "manager-task",
                "state": "running",
                "estimate_seconds": 105,
                "description": "[COMMAND: NEW_SESSION]",
            },
        ):
            self.assertTrue(lifecycle._prepare_accuracy_review_if_due())
        review_id = lifecycle.accuracy_schedule["pending_review"]["review_id"]
        self.register_lifecycle(lifecycle)
        terminal = {
            "tool": "work_update",
            "action": "task/complete",
            "task_id": "manager-task",
            "data": {"body": "Completed."},
        }
        lifecycle.record_work_update(terminal, [terminal], [DONE])

        self.assertFalse(
            long_task_updates.complete_accuracy_review_root(
                "carbon-a",
                review_id,
            )
        )
        self.assertFalse(lifecycle.is_open)
        self.assertIsNone(long_task_updates.current_long_task("carbon-a"))
        self.assertFalse(
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "run-next",
                "event_id: next\nmessage:\nNext request",
                visible=True,
            )
        )

    def test_terminal_accuracy_cleanup_preserves_pending_final_reply(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = time.time() - 6
        lifecycle.queue_final_reply("The release is complete.")
        self.register_lifecycle(lifecycle)
        with mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={
                "task_id": "manager-task",
                "state": "completed",
                "estimate_seconds": 105,
            },
        ):
            self.assertFalse(lifecycle._prepare_accuracy_review_if_due())

        self.assertTrue(lifecycle.is_open)
        self.assertEqual(
            lifecycle.pending_reply["message"],
            "The release is complete.",
        )
        next_context = "event_id: next\nmessage:\nNext request"
        self.assertTrue(
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "run-next",
                next_context,
                visible=True,
            )
        )
        sender = mock.Mock(return_value="Message sent")
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
        ) as execute:
            self.assertEqual(
                lifecycle._flush_final_reply(
                    has_active_workers=False,
                    reply_sender=sender,
                    force=True,
                ),
                "Message sent",
            )

        sender.assert_called_once()
        execute.assert_not_called()
        claimed = long_task_updates.claim_ready_long_task_roots()
        _, clean_context = long_task_updates.extract_queued_long_task_root(
            claimed["carbon-a"]
        )
        self.assertEqual(clean_context, next_context)

    def test_remote_terminal_cancels_obsolete_launched_worker_and_closes(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = time.time() - 6
        lifecycle.journal_worker_start(
            "builder",
            "terminal",
            "Build the release",
        )
        with lifecycle._lock:
            lifecycle.pending_workers["builder"]["phase"] = "launched"
            lifecycle.pending_workers["builder"]["state"] = "in_progress"
            lifecycle._persist(active=True)
        self.register_lifecycle(lifecycle)

        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "manager-task",
                    "state": "completed",
                    "estimate_seconds": 105,
                },
            ),
            mock.patch.object(
                long_task_updates,
                "record_worker_started",
            ) as publish,
        ):
            self.assertFalse(lifecycle._prepare_accuracy_review_if_due())

        publish.assert_not_called()
        self.assertFalse(lifecycle.is_open)
        self.assertEqual(lifecycle.pending_workers, {})
        self.assertIn("builder", lifecycle.worker_delivery_watermarks)
        self.assertIsNone(long_task_updates.current_long_task("carbon-a"))
        self.assertFalse(
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "run-next",
                "event_id: next\nmessage:\nNext request",
                visible=False,
            )
        )

    def test_discarding_final_prepared_worker_retries_terminal_cleanup(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        lifecycle.journal_worker_start(
            "builder",
            "terminal",
            "Build the release",
        )
        with lifecycle._lock:
            lifecycle._terminal = True
            lifecycle._persist(active=True)
        self.register_lifecycle(lifecycle)

        self.assertFalse(lifecycle.close_if_terminal())
        lifecycle.discard_worker_intent("builder")

        self.assertFalse(lifecycle.is_open)
        self.assertEqual(lifecycle.pending_workers, {})
        self.assertIsNone(long_task_updates.current_long_task("carbon-a"))

    def test_worker_delivery_racing_terminal_retries_cleanup_after_drain(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        reference = lifecycle.journal_worker_start(
            "builder",
            "terminal",
            "Build the release",
        )
        with lifecycle._lock:
            lifecycle.pending_workers["builder"]["phase"] = "launched"
            lifecycle.pending_workers["builder"]["state"] = "in_progress"
            lifecycle._persist(active=True)
        self.register_lifecycle(lifecycle)

        def publish_and_terminalize(*_args, **_kwargs):
            with lifecycle._lock:
                lifecycle._terminal = True
                lifecycle._persist(active=True)
            return reference

        with mock.patch.object(
            long_task_updates,
            "record_worker_started",
            side_effect=publish_and_terminalize,
        ):
            delivered = lifecycle._deliver_pending_workers(force=True)

        self.assertEqual(delivered, {"builder": reference})
        self.assertFalse(lifecycle.is_open)
        self.assertEqual(lifecycle.pending_workers, {})
        self.assertIsNone(long_task_updates.current_long_task("carbon-a"))

    def test_settle_guard_clear_retries_terminal_cleanup(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        lifecycle._settle_requested = True
        lifecycle._persist(active=True)
        self.register_lifecycle(lifecycle)

        with mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={
                "task_id": "manager-task",
                "state": "completed",
            },
        ):
            self.assertTrue(lifecycle._settle_task())

        self.assertFalse(lifecycle.is_open)
        self.assertFalse(lifecycle._settle_requested)
        self.assertIsNone(long_task_updates.current_long_task("carbon-a"))

    def test_accuracy_review_survives_restart_and_is_claimed_once(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        anchor = time.time()
        lifecycle.accuracy_schedule["anchor_at"] = anchor
        with (
            mock.patch.object(
                long_task_updates.time,
                "time",
                return_value=anchor + 6,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "manager-task",
                    "state": "running",
                    "estimate_seconds": 105,
                },
            ),
        ):
            self.assertTrue(lifecycle._prepare_accuracy_review_if_due())
        review_id = lifecycle.accuracy_schedule["pending_review"]["review_id"]
        saved = long_task_updates._state_entry("carbon-a")
        lifecycle._closed = True
        lifecycle._stop.set()
        self.expire_lease()

        restarted = long_task_updates.LongTaskLifecycle(
            "carbon-a",
            "ignored-new-run",
            "",
            saved=saved,
            auto_start=False,
        )
        self.register_lifecycle(restarted)
        first = long_task_updates.claim_ready_accuracy_review_roots()
        second = long_task_updates.claim_ready_accuracy_review_roots()

        claimed_id, _ = long_task_updates.extract_accuracy_review_root(
            first["carbon-a"]
        )
        self.assertEqual(claimed_id, review_id)
        self.assertEqual(second, {})
        self.assertEqual(
            restarted.accuracy_schedule["pending_review"]["review_id"],
            review_id,
        )

    def test_restart_before_materialization_coalesces_missed_reviews_once(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        anchor = time.time() - 13 * 5.25
        lifecycle.accuracy_schedule["anchor_at"] = anchor
        lifecycle._persist(active=True)
        self.assertEqual(
            lifecycle.accuracy_schedule["pending_review"],
            {},
        )
        saved = long_task_updates._state_entry("carbon-a")
        lifecycle._closed = True
        lifecycle._stop.set()
        self.expire_lease()
        restarted = long_task_updates.LongTaskLifecycle(
            "carbon-a",
            "ignored-run",
            "",
            saved=saved,
            auto_start=False,
        )
        with mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={
                "task_id": "manager-task",
                "state": "running",
                "estimate_seconds": 105,
            },
        ):
            self.assertTrue(restarted._prepare_accuracy_review_if_due())
            self.assertFalse(restarted._prepare_accuracy_review_if_due())

        pending = restarted.accuracy_schedule["pending_review"]
        self.assertEqual(pending["checkpoint_from"], 1)
        self.assertGreaterEqual(pending["checkpoint_through"], 13)

    def test_final_reply_terminalizes_manager_task_when_transition_was_omitted(self):
        lifecycle = self.lifecycle()
        model_create = {
            "tool": "work_update",
            "action": "task/create",
            "data": {
                "task_id": "manager-task",
                "title": "Ship the release",
                "todos": [
                    {
                        "todo_id": "manager-todo",
                        "title": "Validate the release",
                        "state": "in_progress",
                    }
                ],
            },
        }
        prepared = lifecycle.prepare_work_update(model_create)
        with mock.patch.object(
            long_task_updates,
            "active_task_id",
            return_value="manager-task",
        ):
            lifecycle.record_work_update(model_create, prepared, [DONE])

        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "manager-task",
                    "state": "running",
                    "todos": [
                        {
                            "todo_id": "manager-todo",
                            "state": "in_progress",
                        }
                    ],
                },
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ) as execute,
        ):
            accepted = lifecycle.terminalize_before_reply(
                has_active_workers=False,
            )

        self.assertTrue(accepted)
        self.assertTrue(lifecycle.is_open)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execute.call_args.args[0]["action"], "task/complete")
        self.assertEqual(execute.call_args.args[0]["task_id"], "manager-task")

    def test_final_reply_does_not_terminalize_blocked_manager_task(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "manager-task"
        lifecycle.task_confirmed = True
        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "manager-task",
                    "state": "blocked",
                    "timer_state": "paused",
                    "timer_pause_reason": "blocker",
                },
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
            ) as execute,
        ):
            accepted = lifecycle.terminalize_before_reply(
                has_active_workers=False,
            )

        self.assertFalse(accepted)
        self.assertTrue(lifecycle.is_open)
        self.assertTrue(lifecycle._deferred)
        self.assertTrue(lifecycle._settle_requested)
        execute.assert_not_called()

    def test_later_model_create_updates_manager_task_without_duplicate_card(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            task_id="accepted-task",
            todos=[
                {
                    "todo_id": "accepted-todo",
                    "title": "Initial work",
                    "state": "in_progress",
                }
            ],
        )
        accepted_task_id = lifecycle.task_id

        model_create = {
            "tool": "work_update",
            "action": "task/create",
            "data": {
                "task_id": "manager-release",
                "title": "Ship the release",
                "description": "Build, test, and deploy.",
                "todos": [
                    {
                        "todo_id": "build",
                        "title": "Build",
                        "state": "in_progress",
                    },
                    {
                        "todo_id": "test",
                        "title": "Test",
                        "state": "yet_to_start",
                    },
                ],
            },
        }
        rewritten = lifecycle.prepare_work_update(model_create)

        self.assertEqual(
            [spec["action"] for spec in rewritten],
            ["task/update", "todo/update", "todo/add"],
        )
        self.assertTrue(
            all(spec["task_id"] == accepted_task_id for spec in rewritten)
        )
        self.assertEqual(rewritten[1]["todo_id"], lifecycle.todo_id)
        self.assertNotIn("task/create", [spec["action"] for spec in rewritten])

        later = lifecycle.prepare_work_update(
            {
                "tool": "work_update",
                "action": "todo/update",
                "task_id": "manager-release",
                "todo_id": "build",
                "data": {"state": "completed"},
            }
        )[0]
        self.assertEqual(later["task_id"], accepted_task_id)
        self.assertEqual(later["todo_id"], lifecycle.todo_id)

    def test_explicit_unrelated_task_is_not_redirected(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)

        prepared = lifecycle.prepare_work_update(
            {
                "tool": "work_update",
                "action": "task/update",
                "task_id": "another-task",
                "data": {"description": "Unrelated work"},
            }
        )

        self.assertEqual(prepared[0]["task_id"], "another-task")

    def test_unchanged_durable_heartbeat_does_not_create_new_revision(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            return_value=DONE,
        ) as execute:
            lifecycle._heartbeat("Work is still in progress")
            first_count = execute.call_count
            lifecycle._heartbeat("Work is still in progress")

        self.assertEqual(execute.call_count, first_count)

    def test_lost_heartbeat_response_reconciles_without_second_patch(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        desired = (
            lifecycle.base_description
            + "\n\nLatest activity: Applying the current changes."
        )
        with (
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=ERROR,
            ) as execute,
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                side_effect=[{}, {"state": "running", "description": desired}],
            ),
        ):
            self.assertFalse(
                lifecycle._heartbeat("Applying the current changes")
            )
            self.assertTrue(
                lifecycle._heartbeat("Applying the current changes")
            )

        heartbeat_writes = [
            call
            for call in execute.call_args_list
            if call.args[0]["action"] == "task/update"
        ]
        self.assertEqual(len(heartbeat_writes), 1)

    def test_failed_heartbeat_uses_backoff(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            return_value=ERROR,
        ):
            self.assertFalse(lifecycle._heartbeat("Applying changes"))

        self.assertEqual(lifecycle._heartbeat_attempts, 1)
        self.assertGreater(lifecycle._next_heartbeat_attempt_at, time.time())

    def test_network_create_does_not_hold_lifecycle_state_lock(self):
        lifecycle = self.lifecycle()
        spec = {
            "tool": "work_update",
            "action": "task/create",
            "data": {"task_id": "manager-task", "title": "Manager task"},
        }
        prepared = lifecycle.prepare_work_update(spec)
        lifecycle.record_work_update(spec, prepared, [ERROR])
        lifecycle._next_create_attempt_at = 0
        entered = threading.Event()
        release = threading.Event()

        def slow_create(_spec, _contact_id):
            entered.set()
            release.wait(1)
            return DONE

        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            side_effect=slow_create,
        ):
            thread = threading.Thread(target=lifecycle.ensure)
            thread.start()
            self.assertTrue(entered.wait(1))
            acquired = lifecycle._lock.acquire(timeout=0.1)
            if acquired:
                lifecycle._lock.release()
            release.set()
            thread.join(1)

        self.assertTrue(acquired)
        self.assertFalse(thread.is_alive())

    def test_network_heartbeat_does_not_hold_lifecycle_state_lock(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        entered = threading.Event()
        release = threading.Event()

        def slow_refresh(_contact_id, _task_id):
            entered.set()
            release.wait(1)
            return {"task_id": "task-a", "state": "running"}

        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                side_effect=slow_refresh,
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ),
        ):
            thread = threading.Thread(
                target=lifecycle._heartbeat,
                args=("Applying changes",),
            )
            thread.start()
            self.assertTrue(entered.wait(1))
            acquired = lifecycle._lock.acquire(timeout=0.1)
            if acquired:
                lifecycle._lock.release()
            release.set()
            thread.join(1)

        self.assertTrue(acquired)
        self.assertFalse(thread.is_alive())

    def test_network_settlement_does_not_hold_lifecycle_state_lock(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "manager-task"
        lifecycle.task_confirmed = True
        entered = threading.Event()
        release = threading.Event()

        def slow_refresh(_contact_id, _task_id):
            entered.set()
            release.wait(1)
            return {"task_id": "manager-task", "state": "running"}

        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                side_effect=slow_refresh,
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ),
        ):
            thread = threading.Thread(
                target=lifecycle.terminalize_before_reply,
                kwargs={"has_active_workers": False},
            )
            thread.start()
            self.assertTrue(entered.wait(1))
            acquired = lifecycle._lock.acquire(timeout=0.1)
            if acquired:
                lifecycle._lock.release()
            release.set()
            thread.join(1)

        self.assertTrue(acquired)
        self.assertFalse(thread.is_alive())

    def test_network_worker_card_replay_does_not_hold_lifecycle_state_lock(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        lifecycle.journal_worker_start(
            "builder",
            "terminal",
            "Build it",
        )
        with mock.patch.object(
            long_task_updates,
            "record_worker_started",
            return_value={},
        ):
            lifecycle.mark_worker_started("builder", queued=False)
        entered = threading.Event()
        release = threading.Event()

        def slow_publish(*_args, **_kwargs):
            entered.set()
            release.wait(1)
            return {
                "task_id": "task-a",
                "group_id": lifecycle.pending_workers["builder"]["group_id"],
                "invocation_id": (
                    lifecycle.pending_workers["builder"]["invocation_id"]
                ),
            }

        with mock.patch.object(
            long_task_updates,
            "record_worker_started",
            side_effect=slow_publish,
        ):
            thread = threading.Thread(
                target=lifecycle._deliver_pending_workers,
                kwargs={"force": True},
            )
            thread.start()
            self.assertTrue(entered.wait(1))
            acquired = lifecycle._lock.acquire(timeout=0.1)
            if acquired:
                lifecycle._lock.release()
            release.set()
            thread.join(1)

        self.assertTrue(acquired)
        self.assertFalse(thread.is_alive())

    def test_failed_completion_stays_active_and_retries_stable_intent(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            side_effect=[ERROR, DONE],
        ) as execute:
            accepted = lifecycle.terminalize_before_reply(
                has_active_workers=False,
            )
            self.assertFalse(accepted)
            self.assertTrue(lifecycle.is_open)
            self.assertTrue(lifecycle._settle_requested)

            lifecycle._next_settle_attempt_at = 0
            accepted = lifecycle.terminalize_before_reply(
                has_active_workers=False,
            )

        self.assertTrue(accepted)
        self.assertTrue(lifecycle.is_open)
        terminal_specs = [
            call.args[0]
            for call in execute.call_args_list
            if call.args[0]["action"] == "task/complete"
        ]
        self.assertEqual(len(terminal_specs), 2)
        self.assertEqual(
            terminal_specs[0]["data"]["client_id"],
            terminal_specs[1]["data"]["client_id"],
        )
        self.assertTrue(terminal_specs[0]["data"]["client_id"])

    def test_completion_failure_withholds_reply_then_retries(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)

        with (
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=ERROR,
            ),
            mock.patch.object(
                main,
                "current_long_task",
                return_value=lifecycle,
            ),
            mock.patch.object(
                main,
                "_contact_has_active_workers",
                return_value=False,
            ),
            mock.patch.object(
                main,
                "reply_user",
                return_value="Message sent",
            ) as reply,
            mock.patch.object(main, "send_progress"),
        ):
            main._execute_single_tool(
                {"tool": "reply", "message": "All done."},
                "carbon-a",
            )

        reply.assert_not_called()
        self.assertTrue(lifecycle.is_open)
        self.assertTrue(lifecycle._settle_requested)
        self.assertTrue(lifecycle.pending_reply)

        lifecycle._next_settle_attempt_at = 0
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            return_value=DONE,
        ), mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={"task_id": lifecycle.task_id, "state": "running"},
        ), mock.patch.object(
            main,
            "reply_user",
            return_value="Message sent",
        ) as reply:
            self.assertEqual(
                lifecycle._flush_final_reply(
                    has_active_workers=False,
                    reply_sender=main.reply_user,
                    force=True,
                ),
                "Message sent",
            )
        self.assertFalse(lifecycle.is_open)
        reply.assert_called_once()

    def test_active_worker_prevents_runtime_completion(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            return_value=DONE,
        ) as execute:
            accepted = lifecycle.terminalize_before_reply(
                has_active_workers=True,
            )

        self.assertFalse(accepted)
        self.assertTrue(lifecycle.is_open)
        self.assertFalse(
            any(
                call.args[0]["action"] == "task/complete"
                for call in execute.call_args_list
            )
        )

    def test_deferred_task_remains_active_for_later_resume(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        lifecycle.defer("Provider is rate-limited")
        lifecycle.finish(keep_alive=False)

        self.assertTrue(lifecycle.is_open)
        self.assertTrue(long_task_updates._state_entry("carbon-a")["active"])
        lifecycle.attach("run-b", "continuation", None)
        self.assertFalse(lifecycle._deferred)

    def test_deferred_create_uses_the_actual_rate_limit_pause_reason(self):
        lifecycle = self.lifecycle()
        spec = {
            "tool": "work_update",
            "action": "task/create",
            "data": {"task_id": "manager-task", "title": "Manager task"},
        }
        prepared = lifecycle.prepare_work_update(spec)
        lifecycle.record_work_update(spec, prepared, [ERROR])
        lifecycle._next_create_attempt_at = 0
        lifecycle.defer(
            "Provider is rate-limited",
            pause_reason="rate_limited",
        )
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            side_effect=[DONE, DONE],
        ) as execute, mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={
                "task_id": "manager-task",
                "state": "running",
                "timer_state": "running",
            },
        ):
            lifecycle.ensure("working")
            lifecycle._reconcile_timer(force=True)

        create_data = execute.call_args_list[0].args[0]["data"]
        self.assertNotIn("timer_state", create_data)
        timer_data = execute.call_args_list[1].args[0]["data"]
        self.assertEqual(timer_data["timer_state"], "paused")
        self.assertEqual(
            timer_data["timer_pause_reason"],
            "rate_limited",
        )
        saved = long_task_updates._state_entry("carbon-a")
        self.assertTrue(saved["deferred"])
        self.assertEqual(saved["defer_pause_reason"], "rate_limited")

    def test_final_card_is_attempted_before_the_normal_reply(self):
        lifecycle = mock.Mock()
        order = []
        lifecycle.deliver_final_reply.side_effect = (
            lambda *_args, **_kwargs: order.extend(["terminal", "reply"])
            or "Message sent"
        )
        with (
            mock.patch.object(
                main,
                "current_long_task",
                return_value=lifecycle,
            ),
            mock.patch.object(
                main,
                "_contact_has_active_workers",
                return_value=False,
            ),
            mock.patch.object(
                main,
                "reply_user",
                side_effect=lambda *_args, **_kwargs: (
                    order.append("reply") or "Message sent"
                ),
            ) as reply,
            mock.patch.object(main, "send_progress"),
        ):
            main._execute_single_tool(
                {"tool": "reply", "message": "All done."},
                "carbon-a",
            )

        self.assertEqual(order, ["terminal", "reply"])
        lifecycle.deliver_final_reply.assert_called_once_with(
            "All done.",
            has_active_workers=False,
            reply_sender=reply,
        )

    def test_worker_spawn_ensures_durable_task_before_start(self):
        lifecycle = mock.Mock()
        order = []
        lifecycle.ensure.side_effect = lambda *_args: (
            order.append("ensure") or "task-a"
        )
        lifecycle.journal_worker_start.side_effect = lambda *_args, **_kwargs: (
            order.append("journal")
            or {
                "task_id": "task-a",
                "group_id": "group-a",
                "invocation_id": "invocation-a",
            }
        )
        lifecycle.mark_worker_started.side_effect = lambda *_args, **_kwargs: (
            order.append("publish")
            or {
                "task_id": "task-a",
                "group_id": "group-a",
                "invocation_id": "invocation-a",
            }
        )
        lifecycle.resolve_task_id.return_value = "task-a"
        with (
            mock.patch.object(
                main,
                "current_long_task",
                return_value=lifecycle,
            ),
            mock.patch.object(
                main,
                "start_worker",
                side_effect=lambda *_args, **_kwargs: (
                    order.append("worker") or "Done. started"
                ),
            ),
            mock.patch.object(main, "send_progress"),
        ):
            main._execute_single_tool(
                {
                    "tool": "worker/terminal",
                    "type": "new",
                    "worker-id": "builder",
                    "task": "Build it.",
                },
                "carbon-a",
            )

        self.assertEqual(order, ["ensure", "journal", "worker", "publish"])

    def test_worker_without_manager_task_starts_without_fabricating_a_card(
        self,
    ):
        lifecycle = self.lifecycle()
        with (
            mock.patch.object(
                main,
                "current_long_task",
                return_value=lifecycle,
            ),
            mock.patch.object(
                main,
                "start_worker",
                return_value="Done. started",
            ) as start,
            mock.patch.object(
                main,
                "record_worker_started",
                return_value={},
            ) as publish,
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
            ) as work_update,
            mock.patch.object(main, "send_progress"),
        ):
            result = main._execute_single_tool(
                {
                    "tool": "worker/terminal",
                    "type": "new",
                    "worker-id": "builder",
                    "task": "Build it.",
                },
                "carbon-a",
            )

        self.assertIn("Done. started", result)
        start.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["task_id"], "")
        work_update.assert_not_called()
        self.assertEqual(lifecycle.task_id, "")
        self.assertEqual(lifecycle.todo_id, "")
        self.assertEqual(lifecycle.pending_workers, {})

    def test_worker_starts_with_durable_intent_and_card_replays_after_recovery(self):
        lifecycle = self.lifecycle()
        spec = {
            "tool": "work_update",
            "action": "task/create",
            "data": {"task_id": "manager-task", "title": "Manager task"},
        }
        prepared = lifecycle.prepare_work_update(spec)
        lifecycle.record_work_update(spec, prepared, [ERROR])
        lifecycle._next_create_attempt_at = 0

        def start_worker(*_args, **_kwargs):
            saved = long_task_updates._state_entry("carbon-a")
            self.assertIn("builder", saved["pending_workers"])
            return "Done. started"

        with (
            mock.patch.object(
                main,
                "current_long_task",
                return_value=lifecycle,
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=ERROR,
            ),
            mock.patch.object(
                main,
                "start_worker",
                side_effect=start_worker,
            ) as start,
            mock.patch.object(main, "send_progress"),
        ):
            result = main._execute_single_tool(
                {
                    "tool": "worker/terminal",
                    "type": "new",
                    "worker-id": "builder",
                    "task": "Build it.",
                },
                "carbon-a",
            )

        start.assert_called_once()
        self.assertIn("Done. started", result)
        self.assertIn("durable worker update queued for retry", result)
        self.assertIn("builder", lifecycle.pending_workers)
        intended = dict(lifecycle.pending_workers["builder"])

        lifecycle._next_create_attempt_at = 0
        accepted_reference = {
            "task_id": intended["task_id"],
            "group_id": intended["group_id"],
            "invocation_id": intended["invocation_id"],
        }
        with (
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ),
            mock.patch.object(
                long_task_updates,
                "record_worker_started",
                return_value=accepted_reference,
            ) as publish,
        ):
            self.assertEqual(
                lifecycle.ensure("spawning_worker"),
                intended["task_id"],
            )
            delivered = lifecycle._deliver_pending_workers()

        self.assertEqual(delivered["builder"], accepted_reference)
        self.assertNotIn("builder", lifecycle.pending_workers)
        self.assertEqual(
            publish.call_args.kwargs["invocation_id"],
            intended["invocation_id"],
        )

    def test_terminal_worker_state_updates_persisted_intent_before_replay(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.journal_worker_start(
            "builder",
            "terminal",
            "Build it",
        )

        self.assertTrue(
            long_task_updates.record_pending_worker_state(
                "carbon-a",
                "builder",
                "completed",
                "Build delivered",
            )
        )
        saved = long_task_updates._state_entry("carbon-a")
        intent = saved["pending_workers"]["builder"]
        self.assertEqual(intent["state"], "completed")
        self.assertEqual(intent["state_description"], "Build delivered")

    def test_final_reply_is_executed_after_worker_tools_in_same_batch(self):
        order = []

        def execute(spec, _contact_id):
            order.append(spec["tool"])
            return "Done"

        with mock.patch.object(
            main,
            "execute_single_tool",
            side_effect=execute,
        ):
            main.execute_all_tools(
                [
                    (
                        "carbon-a",
                        {"tool": "reply", "message": "Started."},
                    ),
                    (
                        "carbon-a",
                        {
                            "tool": "worker/terminal",
                            "type": "new",
                            "worker-id": "builder",
                            "task": "Build it.",
                        },
                    ),
                ]
            )

        self.assertEqual(order, ["worker/terminal", "reply"])

    def test_accuracy_review_suppresses_intermediate_reply_tools(self):
        handler = main._make_mid_stream_handler(
            "carbon-a",
            allow_intermediate_replies=False,
        )
        with mock.patch.object(main, "execute_single_tool") as execute:
            handled = handler(
                [
                    {
                        "tool": "reply",
                        "message": "Internal checkpoint.",
                        "work_continues": True,
                    }
                ]
            )

        self.assertEqual(handled, [])
        execute.assert_not_called()

    def test_accuracy_review_turn_executes_only_work_update_and_stays_invisible(
        self,
    ):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = time.time() - 6
        with mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={
                "task_id": "manager-task",
                "state": "running",
                "estimate_seconds": 105,
                "description": "[COMMAND: NEW_SESSION]",
            },
        ):
            self.assertTrue(lifecycle._prepare_accuracy_review_if_due())
        self.register_lifecycle(lifecycle)
        root = long_task_updates.claim_ready_accuracy_review_roots()
        trace = mock.MagicMock()
        trace.trigger = "message"
        trace.run_id = "accuracy-run"
        trace.meta = {}
        output = (
            '{"tools":['
            '{"tool":"reply","message":"internal final"},'
            '{"tool":"message_manager","carbon_id":"peer","message":"noise"},'
            '{"tool":"worker/terminal","type":"new",'
            '"worker-id":"noise-worker","task":"noise"},'
            '{"tool":"work_update","action":"task/update",'
            '"task_id":"manager-task",'
            '"data":{"description":"Accuracy refreshed."}}'
            "]}"
        )
        finished = '{"tools":[{"tool":"do_nothing"}]}'

        with (
            mock.patch.object(main, "handle_commands") as handle_commands,
            mock.patch.object(
                main.Diagnostics,
                "consume_pending_contexts",
                return_value=[],
            ),
            mock.patch.object(
                main.Diagnostics,
                "get_active_run",
                return_value=None,
            ),
            mock.patch.object(
                main.Diagnostics,
                "start_run",
                return_value=trace,
            ),
            mock.patch.object(main.Diagnostics, "register_active"),
            mock.patch.object(main.Diagnostics, "unregister_active"),
            mock.patch.object(
                main,
                "_instrumented_manager_call",
                side_effect=[
                    (output, None, []),
                    (finished, None, []),
                ],
            ),
            mock.patch.object(main, "begin_manager_activity") as begin_activity,
            mock.patch.object(main, "send_progress") as send_progress,
            mock.patch.object(main, "reply_user") as reply,
            mock.patch.object(
                main,
                "execute_work_update",
                return_value=DONE,
            ) as execute_work_update,
            mock.patch.object(main, "send_manager_message") as message_manager,
            mock.patch.object(main, "start_worker") as start_worker,
            mock.patch.object(
                main,
                "_contact_has_active_workers",
                return_value=False,
            ),
        ):
            main.run_all_managers(root)

        begin_activity.assert_not_called()
        handle_commands.assert_not_called()
        send_progress.assert_not_called()
        reply.assert_not_called()
        message_manager.assert_not_called()
        start_worker.assert_not_called()
        execute_work_update.assert_called_once()
        self.assertEqual(
            execute_work_update.call_args.args[0]["action"],
            "task/update",
        )
        self.assertEqual(
            lifecycle.base_description,
            "Accuracy refreshed.",
        )

    def test_forbidden_only_accuracy_output_fails_for_durable_retry(self):
        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = time.time() - 6
        with mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={
                "task_id": "manager-task",
                "state": "running",
                "estimate_seconds": 105,
            },
        ):
            self.assertTrue(lifecycle._prepare_accuracy_review_if_due())
        self.register_lifecycle(lifecycle)
        root = long_task_updates.claim_ready_accuracy_review_roots()
        trace = mock.MagicMock()
        trace.trigger = "manager_loop"
        trace.run_id = "accuracy-run"
        trace.meta = {}
        forbidden = (
            '{"tools":['
            '{"tool":"reply","message":"noise"},'
            '{"tool":"message_manager","carbon_id":"peer","message":"noise"},'
            '{"tool":"worker/terminal","type":"new",'
            '"worker-id":"noise-worker","task":"noise"}'
            "]}"
        )

        with (
            mock.patch.object(main, "handle_commands") as handle_commands,
            mock.patch.object(
                main.Diagnostics,
                "consume_pending_contexts",
                return_value=[],
            ),
            mock.patch.object(
                main.Diagnostics,
                "get_active_run",
                return_value=None,
            ),
            mock.patch.object(
                main.Diagnostics,
                "start_run",
                return_value=trace,
            ),
            mock.patch.object(main.Diagnostics, "register_active"),
            mock.patch.object(main.Diagnostics, "unregister_active"),
            mock.patch.object(
                main,
                "_instrumented_manager_call",
                return_value=(forbidden, None, []),
            ),
            mock.patch.object(main, "reply_user") as reply,
            mock.patch.object(main, "send_manager_message") as message_manager,
            mock.patch.object(main, "start_worker") as start_worker,
            mock.patch.object(
                main,
                "_contact_has_active_workers",
                return_value=False,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "no accepted action",
            ),
        ):
            main.run_all_managers(root)

        handle_commands.assert_not_called()
        reply.assert_not_called()
        message_manager.assert_not_called()
        start_worker.assert_not_called()
        self.assertTrue(lifecycle.accuracy_schedule["pending_review"])

    def test_internal_manager_root_creates_invisible_estimated_lifecycle(self):
        trace = mock.MagicMock()
        trace.trigger = "manager_loop"
        trace.run_id = "internal-run"
        trace.meta = {}
        create = (
            '{"tools":[{"tool":"work_update","action":"task/create",'
            '"data":{"task_id":"internal-task","title":"Internal build",'
            '"realistic_estimate_seconds":100}}]}'
        )
        finished = '{"tools":[{"tool":"do_nothing"}]}'
        with (
            mock.patch.object(
                main,
                "handle_commands",
                side_effect=lambda value: value,
            ),
            mock.patch.object(
                main.Diagnostics,
                "consume_pending_contexts",
                return_value=[],
            ),
            mock.patch.object(
                main.Diagnostics,
                "get_active_run",
                return_value=None,
            ),
            mock.patch.object(
                main.Diagnostics,
                "start_run",
                return_value=trace,
            ),
            mock.patch.object(main.Diagnostics, "register_active"),
            mock.patch.object(main.Diagnostics, "unregister_active"),
            mock.patch.object(
                main,
                "_instrumented_manager_call",
                side_effect=[
                    (create, None, []),
                    (finished, None, []),
                ],
            ),
            mock.patch.object(
                main,
                "execute_work_update",
                return_value=DONE,
            ),
            mock.patch.object(main, "begin_manager_activity") as begin_activity,
            mock.patch.object(main, "send_progress") as send_progress,
            mock.patch.object(
                main,
                "_contact_has_active_workers",
                return_value=False,
            ),
        ):
            main.run_all_managers(
                {"carbon-a": "Internal scheduled maintenance root"}
            )

        lifecycle = long_task_updates.current_long_task("carbon-a")
        self.assertIsNotNone(lifecycle)
        self.assertEqual(lifecycle.task_id, "internal-task")
        self.assertTrue(lifecycle.task_confirmed)
        self.assertEqual(
            lifecycle.accuracy_schedule["goal_seconds"],
            105,
        )
        begin_activity.assert_not_called()
        send_progress.assert_not_called()

    def test_restart_backfills_persisted_active_estimated_task(self):
        old_work_updates_file = work_updates.WORK_UPDATES_FILE
        work_updates.WORK_UPDATES_FILE = (
            Path(self.temp.name) / "work_updates.json"
        )
        self.addCleanup(
            setattr,
            work_updates,
            "WORK_UPDATES_FILE",
            old_work_updates_file,
        )
        work_updates._remember_task(
            "carbon-backfill",
            {
                "task_id": "persisted-task",
                "title": "Persisted task",
                "description": "Continue after deployment.",
                "state": "running",
                "estimate_seconds": 105,
                "active_elapsed_seconds": 52.5,
                "todos": [],
            },
        )
        with (
            mock.patch.object(
                long_task_updates.LongTaskLifecycle,
                "start",
            ) as start,
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
            ) as execute,
        ):
            self.assertEqual(
                long_task_updates.backfill_active_estimated_task_lifecycles(),
                1,
            )
            self.assertEqual(
                long_task_updates.backfill_active_estimated_task_lifecycles(),
                0,
            )

        lifecycle = long_task_updates.current_long_task("carbon-backfill")
        self.assertIsNotNone(lifecycle)
        self.assertEqual(lifecycle.task_id, "persisted-task")
        self.assertTrue(lifecycle.task_confirmed)
        self.assertEqual(
            lifecycle.accuracy_schedule["goal_seconds"],
            105,
        )
        self.assertLess(
            lifecycle.accuracy_schedule["anchor_at"],
            time.time() - 50,
        )
        self.assertGreaterEqual(start.call_count, 1)
        execute.assert_not_called()

    def test_iteration_exhaustion_pauses_task_for_infrastructure(self):
        trace = mock.MagicMock()
        trace.trigger = "message"
        trace.run_id = "run-a"
        trace.meta = {}
        lifecycle = mock.Mock()
        with (
            mock.patch.object(
                main,
                "handle_commands",
                side_effect=lambda value: value,
            ),
            mock.patch.object(
                main.Diagnostics,
                "consume_pending_contexts",
                return_value=[],
            ),
            mock.patch.object(
                main.Diagnostics,
                "get_active_run",
                return_value=None,
            ),
            mock.patch.object(
                main.Diagnostics,
                "start_run",
                return_value=trace,
            ),
            mock.patch.object(main.Diagnostics, "register_active"),
            mock.patch.object(main.Diagnostics, "unregister_active"),
            mock.patch.object(
                main,
                "_instrumented_manager_call",
                return_value=("not tool json", None, []),
            ),
            mock.patch.object(main, "parse_manager_output", return_value=None),
            mock.patch.object(main, "_is_rate_limit", return_value=False),
            mock.patch.object(main, "begin_manager_activity", return_value="group-a"),
            mock.patch.object(main, "settle_manager_activity"),
            mock.patch.object(
                main,
                "begin_long_task_run",
                return_value=lifecycle,
            ),
            mock.patch.object(main, "_contact_has_active_workers", return_value=False),
            mock.patch.object(main, "send_progress"),
            mock.patch.object(main, "set_active_task_timer") as set_timer,
        ):
            main.run_all_managers(
                {
                    "carbon-a": (
                        "room_id: room-a\nevent_id: event-a\n"
                        "message:\nKeep working"
                    )
                }
            )

        set_timer.assert_not_called()
        lifecycle.defer.assert_called_with(
            "Work paused after the manager retry budget was exhausted"
        )

    def test_public_refresh_reconciles_the_local_task_cache(self):
        snapshot = {
            "task_id": "task-a",
            "state": "running",
            "revision": 7,
            "todos": [],
        }
        client = mock.Mock()
        client.work_task_show.return_value = {"data": snapshot}
        with mock.patch.object(work_updates, "_remember_task") as remember:
            result = work_updates.refresh_task_snapshot(
                "carbon-a",
                "task-a",
                client=client,
            )

        self.assertEqual(result, snapshot)
        client.work_task_show.assert_called_once_with("task-a")
        remember.assert_called_once_with("carbon-a", snapshot)

    def test_terminal_response_loss_is_proven_before_idempotent_reply(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)

        order = []

        def execute(spec, _contact_id):
            order.append(spec["action"])
            return ERROR

        def reply(*_args, **_kwargs):
            order.append("reply")
            return "Message sent"

        with (
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                side_effect=execute,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                side_effect=[
                    {
                        "task_id": lifecycle.task_id,
                        "state": "running",
                        "todos": [
                            {
                                "todo_id": lifecycle.todo_id,
                                "state": "completed",
                            }
                        ],
                    },
                    {
                        "task_id": lifecycle.task_id,
                        "state": "completed",
                    },
                ],
            ),
        ):
            status = lifecycle.deliver_final_reply(
                "Release shipped.",
                has_active_workers=False,
                reply_sender=reply,
            )

        self.assertEqual(status, "Message sent")
        self.assertEqual(order, ["task/complete", "reply"])

    def test_lost_reply_response_retries_same_client_id(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)

        seen_client_ids = []

        def lost_reply(*_args, **kwargs):
            seen_client_ids.append(kwargs["client_id"])
            return "Interface unavailable"

        with (
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": lifecycle.task_id,
                    "state": "running",
                    "todos": [
                        {
                            "todo_id": lifecycle.todo_id,
                            "state": "completed",
                        }
                    ],
                },
            ),
        ):
            self.assertEqual(
                lifecycle.deliver_final_reply(
                    "Release shipped.",
                    has_active_workers=False,
                    reply_sender=lost_reply,
                ),
                "Message queued for durable delivery",
            )

        def accepted_reply(*_args, **kwargs):
            seen_client_ids.append(kwargs["client_id"])
            return "Message sent"

        self.assertEqual(
            lifecycle._flush_final_reply(
                has_active_workers=False,
                reply_sender=accepted_reply,
                force=True,
            ),
            "Message sent",
        )
        self.assertEqual(len(seen_client_ids), 2)
        self.assertEqual(seen_client_ids[0], seen_client_ids[1])

    def test_non_retryable_final_reply_error_closes_without_replaying(self):
        lifecycle = self.lifecycle()
        sender = mock.Mock(
            return_value=(
                "Sent with errors: text segment failed: api 409: "
                '{"code":"idempotency_conflict","retryable":false}'
            )
        )

        self.assertEqual(
            lifecycle.deliver_final_reply(
                "Release shipped. [file=/tmp/evidence.md]",
                has_active_workers=False,
                reply_sender=sender,
            ),
            "Message delivery abandoned",
        )

        self.assertFalse(lifecycle.is_open)
        self.assertFalse(lifecycle.pending_reply)
        sender.assert_called_once()

    def test_final_reply_retry_budget_prevents_unbounded_replay(self):
        lifecycle = self.lifecycle()
        sender = mock.Mock(return_value="Interface unavailable")

        with mock.patch.object(
            long_task_updates,
            "MAX_PENDING_REPLY_ATTEMPTS",
            2,
        ):
            self.assertEqual(
                lifecycle.deliver_final_reply(
                    "Release shipped.",
                    has_active_workers=False,
                    reply_sender=sender,
                ),
                "Message queued for durable delivery",
            )
            self.assertEqual(
                lifecycle._flush_final_reply(
                    has_active_workers=False,
                    reply_sender=sender,
                    force=True,
                ),
                "Message delivery abandoned",
            )

        self.assertFalse(lifecycle.is_open)
        self.assertFalse(lifecycle.pending_reply)
        self.assertEqual(sender.call_count, 2)

    def test_concurrent_reply_replay_does_not_resurrect_settled_state(self):
        lifecycle = self.lifecycle()
        lifecycle.queue_final_reply("Exactly once.")
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def sender(*_args, **kwargs):
            calls.append(kwargs["client_id"])
            entered.set()
            release.wait(1)
            return "Message sent"

        results = []

        def flush():
            results.append(
                lifecycle._flush_final_reply(
                    has_active_workers=False,
                    reply_sender=sender,
                    force=True,
                )
            )

        first = threading.Thread(target=flush)
        second = threading.Thread(target=flush)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        release.set()
        first.join(1)
        second.join(1)

        self.assertEqual(calls, [calls[0]])
        self.assertEqual(results.count("Message sent"), 2)
        self.assertFalse(long_task_updates._state_entry("carbon-a")["active"])

    def test_boot_recovery_replays_create_settle_and_reply_without_ingress(self):
        lifecycle = self.lifecycle()
        lifecycle.started_at = time.time() - 30
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            return_value=ERROR,
        ):
            lifecycle.deliver_final_reply(
                "Recovered reply.",
                has_active_workers=False,
                reply_sender=mock.Mock(),
            )
        lifecycle._closed = True
        lifecycle._stop.set()

        def make_due(state):
            entry = state["contacts"]["carbon-a"]
            entry["next_create_attempt_at"] = 0
            entry["next_settle_attempt_at"] = 0
            entry["lease_until"] = 0
            entry["lease_pid"] = 0

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            make_due,
        )
        sender = mock.Mock(return_value="Message sent")
        with (
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": lifecycle.task_id,
                    "state": "running",
                    "todos": [
                        {
                            "todo_id": lifecycle.todo_id,
                            "state": "completed",
                        }
                    ],
                },
            ),
        ):
            recovered = long_task_updates.recover_long_task_lifecycles(
                reply_sender=sender,
                has_active_workers=lambda _contact_id: False,
                limit=1,
            )
            deadline = time.time() + 2
            while sender.call_count == 0 and time.time() < deadline:
                time.sleep(0.01)

        self.assertEqual(recovered, 1)
        sender.assert_called_once()
        self.assertFalse(long_task_updates._state_entry("carbon-a")["active"])

    def test_recovery_replays_settlement_without_new_manager_message(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        lifecycle._settle_requested = True
        lifecycle._persist(active=True)
        lifecycle._closed = True
        lifecycle._stop.set()
        self.expire_lease()

        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={"task_id": "task-a", "state": "running"},
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ) as execute,
        ):
            self.assertEqual(
                long_task_updates.recover_long_task_lifecycles(limit=1),
                1,
            )
            deadline = time.time() + 2
            while (
                not any(
                    call.args[0]["action"] == "task/complete"
                    for call in execute.call_args_list
                )
                and time.time() < deadline
            ):
                time.sleep(0.01)

        self.assertTrue(
            any(
                call.args[0]["action"] == "task/complete"
                for call in execute.call_args_list
            )
        )

    def test_boot_recovery_replays_timer_intent_without_ingress(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        lifecycle.defer(
            "Provider rate limited", pause_reason="rate_limited"
        )
        lifecycle._closed = True
        lifecycle._stop.set()
        self.expire_lease()
        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "task-a",
                    "state": "running",
                    "timer_state": "running",
                },
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ) as execute,
        ):
            self.assertEqual(
                long_task_updates.recover_long_task_lifecycles(limit=1),
                1,
            )
            deadline = time.time() + 2
            while (
                not any(
                    call.args[0].get("data", {}).get("timer_state")
                    == "paused"
                    for call in execute.call_args_list
                )
                and time.time() < deadline
            ):
                time.sleep(0.01)

        timer_call = next(
            call
            for call in execute.call_args_list
            if call.args[0].get("data", {}).get("timer_state") == "paused"
        )
        self.assertEqual(
            timer_call.args[0]["data"]["timer_pause_reason"],
            "rate_limited",
        )

    def test_boot_recovery_starts_transport_replay_without_blocking_startup(self):
        lifecycles = []
        for index in range(4):
            contact_id = f"carbon-{index}"
            lifecycle = long_task_updates.LongTaskLifecycle(
                contact_id,
                f"run-{index}",
                f"message:\nRecover request {index}",
                auto_start=False,
            )
            lifecycle.task_id = f"task-{index}"
            lifecycle.task_confirmed = True
            lifecycle._settle_requested = True
            lifecycle._persist(active=True)
            lifecycle._closed = True
            lifecycle._stop.set()
            self.expire_lease(contact_id)
            lifecycles.append(lifecycle)

        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def blocking_refresh(_contact_id, task_id):
            nonlocal calls
            with calls_lock:
                calls += 1
                entered.set()
            self.assertTrue(release.wait(2))
            return {"task_id": task_id, "state": "running"}

        try:
            with (
                mock.patch.object(
                    long_task_updates,
                    "refresh_task_snapshot",
                    side_effect=blocking_refresh,
                ),
                mock.patch.object(
                    long_task_updates,
                    "execute_work_update",
                    return_value=DONE,
                ),
            ):
                started_at = time.monotonic()
                recovered = long_task_updates.recover_long_task_lifecycles(
                    limit=len(lifecycles)
                )
                elapsed = time.monotonic() - started_at

                self.assertEqual(recovered, len(lifecycles))
                self.assertLess(elapsed, 0.5)
                self.assertTrue(entered.wait(1))
        finally:
            with long_task_updates._REGISTRY_LOCK:
                recovered_lifecycles = list(
                    long_task_updates._ACTIVE_BY_CONTACT.values()
                )
            for lifecycle in recovered_lifecycles:
                lifecycle._stop.set()
            release.set()
            for lifecycle in recovered_lifecycles:
                if lifecycle._thread is not None:
                    lifecycle._thread.join(2)

    def test_boot_recovery_reconciles_and_publishes_launched_worker(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        reference = lifecycle.journal_worker_start(
            "builder", "terminal", "Build the release"
        )
        lifecycle._closed = True
        lifecycle._stop.set()
        self.expire_lease()
        with mock.patch.object(
            long_task_updates,
            "record_worker_started",
            return_value=reference,
        ) as publish:
            self.assertEqual(
                long_task_updates.recover_long_task_lifecycles(
                    worker_status_resolver=lambda _worker_id, _contact_id: (
                        "Worker 'builder' (terminal, codex) status: running"
                    ),
                    limit=1,
                ),
                1,
            )
            deadline = time.time() + 2
            while publish.call_count == 0 and time.time() < deadline:
                time.sleep(0.01)

        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["state_name"], "in_progress")

    def test_unrelated_root_waits_for_prior_reply_then_gets_distinct_task(self):
        prior = self.lifecycle()
        prior.task_id = "task-prior"
        prior.todo_id = "todo-prior"
        prior.task_confirmed = True
        prior.queue_final_reply("First response.")

        second_context = (
            "room_id: room-a\n"
            "event_id: event-b\n"
            "message:\nInvestigate an unrelated issue"
        )
        self.assertTrue(
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "run-b",
                second_context,
                visible=True,
            )
        )
        self.assertEqual(
            long_task_updates.claim_ready_long_task_roots(),
            {},
        )

        sent = []

        def sender(message, _contact_id, **_kwargs):
            sent.append(message)
            return "Message sent"

        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={"task_id": "task-prior", "state": "running"},
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ),
        ):
            self.assertEqual(
                prior._flush_final_reply(
                    has_active_workers=False,
                    reply_sender=sender,
                    force=True,
                ),
                "Message sent",
            )

        claimed = long_task_updates.claim_ready_long_task_roots()
        self.assertEqual(set(claimed), {"carbon-a"})
        root_id, clean_context, durable_visibility = (
            long_task_updates.extract_queued_long_task_root_metadata(
                claimed["carbon-a"]
            )
        )
        self.assertTrue(root_id)
        self.assertTrue(durable_visibility)
        self.assertEqual(clean_context, second_context)

        second = long_task_updates.begin_long_task_run(
            "carbon-a",
            "run-b",
            clean_context,
            visible=True,
            reply_sender=sender,
        )
        self.assertIsNotNone(second)
        long_task_updates.acknowledge_queued_long_task_root(root_id)
        self.accept_task(second, task_id="task-second")
        second_task_id = second.task_id
        self.assertNotEqual(second_task_id, prior.task_id)
        self.assertEqual(second.run_id, "run-b")
        with mock.patch.object(
            long_task_updates,
            "execute_work_update",
            return_value=DONE,
        ):
            self.assertEqual(
                second.deliver_final_reply(
                    "Second response.",
                    has_active_workers=False,
                    reply_sender=sender,
                ),
                "Message sent",
            )

        self.assertEqual(sent, ["First response.", "Second response."])
        self.assertEqual(
            long_task_updates.claim_ready_long_task_roots(),
            {},
        )

    def test_invisible_root_uses_same_pending_final_delivery_fence(self):
        prior = self.lifecycle()
        prior.task_id = "task-prior"
        prior.task_confirmed = True
        prior.queue_final_reply("First response.")
        internal_context = (
            "Internal manager handoff.\n"
            "Reconcile the next background request"
        )

        self.assertTrue(
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "internal-run-b",
                internal_context,
                visible=False,
            )
        )
        state = long_task_updates.read_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
        )
        self.assertFalse(
            state["queued_roots"]["carbon-a"][0]["visible"]
        )
        self.assertEqual(
            long_task_updates.claim_ready_long_task_roots(),
            {},
        )

        sender = mock.Mock(return_value="Message sent")
        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "task-prior",
                    "state": "running",
                },
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=DONE,
            ),
        ):
            self.assertEqual(
                prior._flush_final_reply(
                    has_active_workers=False,
                    reply_sender=sender,
                    force=True,
                ),
                "Message sent",
            )

        claimed = long_task_updates.claim_ready_long_task_roots()
        self.assertEqual(set(claimed), {"carbon-a"})
        root_id, clean_context, durable_visibility = (
            long_task_updates.extract_queued_long_task_root_metadata(
                claimed["carbon-a"]
            )
        )
        self.assertTrue(root_id)
        self.assertFalse(durable_visibility)
        self.assertEqual(clean_context, internal_context)
        self.assertNotIn(internal_context, prior.base_description)

    def test_visible_queued_root_replay_honors_durable_visibility(self):
        context = (
            f"{long_task_updates._QUEUED_ROOT_MARKER} root-visible\n"
            f"{long_task_updates._QUEUED_ROOT_VISIBILITY_MARKER} 1\n"
            "Internal handoff without replayable room metadata"
        )
        trace = mock.MagicMock()
        trace.trigger = "manager_loop"
        trace.run_id = "fresh-replay-run"
        trace.meta = {}
        with (
            mock.patch.object(
                main,
                "handle_commands",
                side_effect=lambda contexts: dict(contexts),
            ),
            mock.patch.object(
                main.Diagnostics,
                "consume_pending_contexts",
                return_value=[],
            ),
            mock.patch.object(
                main.Diagnostics,
                "get_active_run",
                return_value=None,
            ),
            mock.patch.object(
                main.Diagnostics,
                "start_run",
                return_value=trace,
            ),
            mock.patch.object(main.Diagnostics, "register_active"),
            mock.patch.object(main.Diagnostics, "unregister_active"),
            mock.patch.object(
                main,
                "_work_lifecycle_is_visible",
                return_value=False,
            ) as infer_visibility,
            mock.patch.object(
                main,
                "_instrumented_manager_call",
                return_value=(
                    '{"tools":[{"tool":"do_nothing"}]}',
                    None,
                    [],
                ),
            ),
            mock.patch.object(
                main,
                "begin_manager_activity",
                return_value="visible-group",
            ) as begin_activity,
            mock.patch.object(main, "send_progress") as send_progress,
            mock.patch.object(main, "settle_manager_activity"),
            mock.patch.object(
                main,
                "_contact_has_active_workers",
                return_value=False,
            ),
        ):
            main.run_all_managers({"carbon-a": context})

        infer_visibility.assert_not_called()
        begin_activity.assert_called_once()
        self.assertTrue(send_progress.called)

    def test_legacy_queued_root_marker_keeps_visibility_fallback(self):
        root_id, clean_context, durable_visibility = (
            long_task_updates.extract_queued_long_task_root_metadata(
                f"{long_task_updates._QUEUED_ROOT_MARKER} legacy-root\n"
                "Legacy queued context"
            )
        )

        self.assertEqual(root_id, "legacy-root")
        self.assertEqual(clean_context, "Legacy queued context")
        self.assertIsNone(durable_visibility)

    def test_invisible_queued_root_replay_honors_durable_visibility(self):
        context = (
            f"{long_task_updates._QUEUED_ROOT_MARKER} root-invisible\n"
            f"{long_task_updates._QUEUED_ROOT_VISIBILITY_MARKER} 0\n"
            "Internal handoff that must stay silent"
        )
        trace = mock.MagicMock()
        trace.trigger = "message"
        trace.run_id = "fresh-replay-run"
        trace.meta = {}
        with (
            mock.patch.object(
                main,
                "handle_commands",
                side_effect=lambda contexts: dict(contexts),
            ),
            mock.patch.object(
                main.Diagnostics,
                "consume_pending_contexts",
                return_value=[],
            ),
            mock.patch.object(
                main.Diagnostics,
                "get_active_run",
                return_value=None,
            ),
            mock.patch.object(
                main.Diagnostics,
                "start_run",
                return_value=trace,
            ),
            mock.patch.object(main.Diagnostics, "register_active"),
            mock.patch.object(main.Diagnostics, "unregister_active"),
            mock.patch.object(
                main,
                "_work_lifecycle_is_visible",
                return_value=True,
            ) as infer_visibility,
            mock.patch.object(
                main,
                "_instrumented_manager_call",
                return_value=(
                    '{"tools":[{"tool":"do_nothing"}]}',
                    None,
                    [],
                ),
            ),
            mock.patch.object(main, "begin_manager_activity") as begin_activity,
            mock.patch.object(main, "send_progress") as send_progress,
            mock.patch.object(main, "settle_manager_activity"),
            mock.patch.object(
                main,
                "_contact_has_active_workers",
                return_value=False,
            ),
        ):
            main.run_all_managers({"carbon-a": context})

        infer_visibility.assert_not_called()
        begin_activity.assert_not_called()
        send_progress.assert_not_called()

    def test_dispatcher_retry_owns_claimed_root_after_long_task_ack(self):
        from core.maintenance import MaintenanceCoordinator

        prior = self.lifecycle()
        prior.task_id = "task-prior"
        prior.task_confirmed = True
        prior.queue_final_reply("First response.")
        second_context = (
            "room_id: room-a\n"
            "event_id: event-b\n"
            "message:\nRun the queued request"
        )
        self.assertTrue(
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "run-b",
                second_context,
                visible=True,
            )
        )
        with prior._lock:
            prior._close_locked()
        claimed = long_task_updates.claim_ready_long_task_roots()
        self.assertEqual(set(claimed), {"carbon-a"})

        clock = [10_000.0]
        maintenance_root = Path(self.temp.name) / "maintenance-root"
        maintenance_root.mkdir()
        coordinator = MaintenanceCoordinator(
            maintenance_root,
            state_file=Path(self.temp.name) / "maintenance.json",
            clock=lambda: clock[0],
        )
        calls = []

        def runner(payload):
            context = payload["carbon-a"]
            root_id, clean_context = (
                long_task_updates.extract_queued_long_task_root(context)
            )
            calls.append((root_id, clean_context))
            if len(calls) == 1:
                raise RuntimeError("process failed after queue acknowledgement")

        dispatcher = main.ManagerDispatcher(runner=runner)
        try:
            with mock.patch.object(main, "MAINTENANCE", coordinator):
                dispatcher.submit(claimed)
                # submit acknowledged only after enqueue_root durably admitted
                # the exact marked context.
                self.assertEqual(
                    long_task_updates.claim_ready_long_task_roots(),
                    {},
                )
                self.assertTrue(dispatcher.wait_for_idle(2))
                self.assertEqual(
                    coordinator.public_status()["queued_message_count"],
                    1,
                )

                clock[0] += 6
                self.assertEqual(dispatcher.replay_maintenance_queue(), 1)
                self.assertTrue(dispatcher.wait_for_idle(2))
                self.assertEqual(
                    coordinator.public_status()["queued_message_count"],
                    0,
                )
        finally:
            dispatcher.shutdown(wait=True)

        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][1], second_context)

    def test_tick_claims_accuracy_review_and_dispatcher_retries_exact_root(
        self,
    ):
        from core.maintenance import MaintenanceCoordinator

        lifecycle = self.lifecycle()
        self.accept_task(
            lifecycle,
            realistic_estimate_seconds=100,
        )
        lifecycle.accuracy_schedule["anchor_at"] = 1_000.0
        with (
            mock.patch.object(
                long_task_updates.time,
                "time",
                return_value=1_006.0,
            ),
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "manager-task",
                    "state": "running",
                    "estimate_seconds": 105,
                },
            ),
        ):
            self.assertTrue(lifecycle._prepare_accuracy_review_if_due())
        self.register_lifecycle(lifecycle)

        with mock.patch.object(
            main,
            "claim_ready_long_task_roots",
            return_value={},
        ):
            excluded = main._merge_due_internal_roots(
                {"carbon-a": "event_id: user-root"},
                maintenance_active=False,
            )
            self.assertEqual(
                excluded,
                {"carbon-a": "event_id: user-root"},
            )
            claimed = main._merge_due_internal_roots(
                {"carbon-user": "event_id: another-root"},
                maintenance_active=False,
            )

        self.assertEqual(set(claimed), {"carbon-user", "carbon-a"})
        review_id, _ = long_task_updates.extract_accuracy_review_root(
            claimed["carbon-a"]
        )
        self.assertTrue(review_id)

        clock = [10_000.0]
        maintenance_root = Path(self.temp.name) / "accuracy-maintenance-root"
        maintenance_root.mkdir()
        coordinator = MaintenanceCoordinator(
            maintenance_root,
            state_file=Path(self.temp.name) / "accuracy-maintenance.json",
            clock=lambda: clock[0],
        )
        calls = []

        def runner(payload):
            calls.append(payload["carbon-a"])
            if len(calls) == 1:
                raise RuntimeError("accuracy manager interrupted")

        dispatcher = main.ManagerDispatcher(runner=runner)
        try:
            with mock.patch.object(main, "MAINTENANCE", coordinator):
                dispatcher.submit({"carbon-a": claimed["carbon-a"]})
                self.assertTrue(dispatcher.wait_for_idle(2))
                pending = lifecycle.accuracy_schedule["pending_review"]
                self.assertEqual(pending["review_id"], review_id)
                self.assertEqual(pending["phase"], "dispatched")
                self.assertEqual(
                    coordinator.public_status()["queued_message_count"],
                    1,
                )

                clock[0] += 6
                self.assertEqual(dispatcher.replay_maintenance_queue(), 1)
                self.assertTrue(dispatcher.wait_for_idle(2))
        finally:
            dispatcher.shutdown(wait=True)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(lifecycle.accuracy_schedule["pending_review"], {})
        self.assertEqual(lifecycle.accuracy_schedule["next_checkpoint"], 2)
        self.assertEqual(
            coordinator.public_status()["queued_message_count"],
            0,
        )

    def test_live_queued_root_claim_is_not_resubmitted_by_same_process(self):
        prior = self.lifecycle()
        prior.task_id = "task-prior"
        prior.task_confirmed = True
        prior.queue_final_reply("First response.")
        self.assertTrue(
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "run-b",
                "event_id: event-b\nmessage:\nSecond request",
                visible=True,
            )
        )
        with prior._lock:
            prior._close_locked()

        first = long_task_updates.claim_ready_long_task_roots()
        second = long_task_updates.claim_ready_long_task_roots()

        self.assertEqual(set(first), {"carbon-a"})
        self.assertEqual(second, {})

    def test_finish_closes_terminal_task_even_when_task_id_remains(self):
        lifecycle = self.lifecycle()
        self.accept_task(lifecycle)
        with lifecycle._lock:
            lifecycle._terminal = True
            lifecycle._persist(active=True)
        self.register_lifecycle(lifecycle)

        lifecycle.finish()

        self.assertFalse(lifecycle.is_open)
        self.assertIsNone(long_task_updates.current_long_task("carbon-a"))
        self.assertFalse(
            long_task_updates._state_entry("carbon-a").get("active")
        )

    def test_boot_recovery_reaps_expired_empty_ephemeral_lifecycle(self):
        now = time.time()

        def fill(state):
            state["contacts"]["carbon-a"] = {
                "active": True,
                "contact_id": "carbon-a",
                "run_id": "stale-run",
                "task_id": "",
                "terminal": False,
                "manager_running": False,
                "deferred": True,
                "pending_reply": {},
                "pending_workers": {},
                "pending_create_spec": {},
                "settle_requested": False,
                "lease_owner": "stale-owner",
                "lease_pid": os.getpid(),
                "lease_until": now - 1,
                "updated_at": now - 31,
            }

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            fill,
        )

        recovered = long_task_updates.recover_long_task_lifecycles()

        self.assertEqual(recovered, 0)
        self.assertFalse(
            long_task_updates._state_entry("carbon-a").get("active")
        )
        self.assertIsNone(long_task_updates.current_long_task("carbon-a"))

    def test_boot_recovery_preserves_live_empty_ephemeral_lifecycle(self):
        now = time.time()

        def fill(state):
            state["contacts"]["carbon-a"] = {
                "active": True,
                "contact_id": "carbon-a",
                "run_id": "live-run",
                "task_id": "",
                "terminal": False,
                "manager_running": False,
                "pending_reply": {},
                "pending_workers": {},
                "pending_create_spec": {},
                "settle_requested": False,
                "lease_owner": f"{long_task_updates._PROCESS_TOKEN}:live-owner",
                "lease_pid": os.getpid(),
                "lease_until": now + 60,
                "updated_at": now,
            }

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            fill,
        )

        recovered = long_task_updates.recover_long_task_lifecycles()

        self.assertEqual(recovered, 0)
        self.assertTrue(
            long_task_updates._state_entry("carbon-a").get("active")
        )

    def test_boot_recovery_reaps_reused_pid_from_prior_process(self):
        now = time.time()

        def fill(state):
            state["contacts"]["carbon-a"] = {
                "active": True,
                "contact_id": "carbon-a",
                "run_id": "stale-run",
                "task_id": "",
                "terminal": False,
                "manager_running": False,
                "pending_reply": {},
                "pending_workers": {},
                "pending_create_spec": {},
                "settle_requested": False,
                "lease_owner": "prior-process-token:stale-owner",
                "lease_pid": os.getpid(),
                "lease_until": now + 60,
                "updated_at": now,
            }

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            fill,
        )

        recovered = long_task_updates.recover_long_task_lifecycles()

        self.assertEqual(recovered, 0)
        self.assertFalse(
            long_task_updates._state_entry("carbon-a").get("active")
        )

    def test_expired_terminal_fence_is_reaped_before_oldest_root_claim(self):
        now = time.time()

        def fill(state):
            state["contacts"]["carbon-a"] = {
                "active": True,
                "contact_id": "carbon-a",
                "run_id": "stale-run",
                "task_id": "stale-task",
                "terminal": True,
                "manager_running": True,
                "pending_reply": {},
                "pending_workers": {},
                "pending_create_spec": {},
                "settle_requested": False,
                "lease_owner": "stale-owner",
                "lease_pid": os.getpid(),
                "lease_until": now - 1,
                "updated_at": now - 31,
            }
            state["queued_roots"]["carbon-a"] = [
                {
                    "root_id": "queued-root:first",
                    "run_id": "run-first",
                    "context": "message:\nFirst request",
                    "created_at": now - 20,
                    "claim_owner": "",
                    "claim_pid": 0,
                    "claim_until": 0.0,
                },
                {
                    "root_id": "queued-root:second",
                    "run_id": "run-second",
                    "context": "message:\nSecond request",
                    "created_at": now - 10,
                    "claim_owner": "",
                    "claim_pid": 0,
                    "claim_until": 0.0,
                },
            ]

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            fill,
        )

        claimed = long_task_updates.claim_ready_long_task_roots()

        self.assertEqual(set(claimed), {"carbon-a"})
        root_id, context = long_task_updates.extract_queued_long_task_root(
            claimed["carbon-a"]
        )
        self.assertEqual(root_id, "queued-root:first")
        self.assertEqual(context, "message:\nFirst request")
        self.assertFalse(
            long_task_updates._state_entry("carbon-a").get("active")
        )

    def test_live_terminal_lease_is_not_reaped(self):
        now = time.time()

        def fill(state):
            state["contacts"]["carbon-a"] = {
                "active": True,
                "contact_id": "carbon-a",
                "run_id": "live-run",
                "terminal": True,
                "pending_reply": {},
                "pending_workers": {},
                "pending_create_spec": {},
                "settle_requested": False,
                "lease_owner": "live-owner",
                "lease_pid": os.getpid(),
                "lease_until": now + 60,
                "updated_at": now,
            }
            state["queued_roots"]["carbon-a"] = [
                {
                    "root_id": "queued-root:next",
                    "run_id": "run-next",
                    "context": "message:\nNext request",
                    "created_at": now,
                    "claim_owner": "",
                    "claim_pid": 0,
                    "claim_until": 0.0,
                }
            ]

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            fill,
        )

        self.assertEqual(long_task_updates.claim_ready_long_task_roots(), {})
        self.assertTrue(
            long_task_updates._state_entry("carbon-a").get("active")
        )

    def test_new_root_stays_behind_backlog_after_terminal_recovery(self):
        now = time.time()

        def fill(state):
            state["contacts"]["carbon-a"] = {
                "active": True,
                "contact_id": "carbon-a",
                "run_id": "stale-run",
                "terminal": True,
                "pending_reply": {},
                "pending_workers": {},
                "pending_create_spec": {},
                "settle_requested": False,
                "lease_owner": "stale-owner",
                "lease_pid": 0,
                "lease_until": 0.0,
                "updated_at": now - 60,
            }
            state["queued_roots"]["carbon-a"] = [
                {
                    "root_id": "queued-root:first",
                    "run_id": "run-first",
                    "context": "message:\nFirst request",
                    "created_at": now - 30,
                    "claim_owner": "",
                    "claim_pid": 0,
                    "claim_until": 0.0,
                }
            ]

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            fill,
        )

        self.assertTrue(
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "run-second",
                "message:\nSecond request",
                visible=True,
            )
        )

        state = long_task_updates.read_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
        )
        self.assertFalse(state["contacts"]["carbon-a"]["active"])
        self.assertEqual(
            [
                item["run_id"]
                for item in state["queued_roots"]["carbon-a"]
            ],
            ["run-first", "run-second"],
        )

    def test_closed_lifecycle_cannot_resurrect_its_tombstone(self):
        lifecycle = self.lifecycle()
        with lifecycle._lock:
            lifecycle._close_locked()

        self.assertFalse(lifecycle._persist(active=True))
        self.assertFalse(
            long_task_updates._state_entry("carbon-a").get("active")
        )

    def test_full_queued_root_journal_fails_closed_without_dropping(self):
        now = time.time()

        def fill(state):
            state["contacts"]["carbon-a"] = {
                "active": True,
                "contact_id": "carbon-a",
                "run_id": "run-a",
                "pending_reply": {"message": "First response."},
                "updated_at": now,
            }
            state["queued_roots"]["carbon-a"] = [
                {
                    "root_id": f"queued-root:{index}",
                    "run_id": f"run-{index}",
                    "context": f"message:\nRequest {index}",
                    "created_at": now + index,
                    "claim_owner": "",
                    "claim_pid": 0,
                    "claim_until": 0.0,
                }
                for index in range(
                    long_task_updates.MAX_QUEUED_ROOTS_PER_CONTACT
                )
            ]

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            fill,
        )

        with self.assertRaisesRegex(RuntimeError, "queue is at capacity"):
            long_task_updates.queue_long_task_root_if_blocked(
                "carbon-a",
                "overflow-run",
                "message:\nMust be retried by maintenance",
                visible=True,
            )

        state = long_task_updates.read_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
        )
        queued = state["queued_roots"]["carbon-a"]
        self.assertEqual(
            len(queued),
            long_task_updates.MAX_QUEUED_ROOTS_PER_CONTACT,
        )
        self.assertFalse(
            any(item.get("run_id") == "overflow-run" for item in queued)
        )

    def test_live_lease_blocks_second_process_and_expiry_allows_takeover(self):
        first = self.lifecycle()
        saved = long_task_updates._state_entry("carbon-a")
        second = long_task_updates.LongTaskLifecycle(
            "carbon-a",
            "run-b",
            "message:\nOther process",
            saved=saved,
            auto_start=False,
        )
        self.assertFalse(second.is_open)

        first._closed = True
        first._stop.set()
        self.expire_lease()
        third = long_task_updates.LongTaskLifecycle(
            "carbon-a",
            "run-c",
            "message:\nRecovered process",
            saved=long_task_updates._state_entry("carbon-a"),
            auto_start=False,
        )
        self.assertTrue(third.is_open)
        self.assertEqual(third.run_id, "run-a")

    def test_prepared_worker_is_never_published_before_launch(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        lifecycle.journal_worker_start(
            "builder", "terminal", "Build the release"
        )
        with mock.patch.object(
            long_task_updates,
            "record_worker_started",
        ) as publish:
            lifecycle._deliver_pending_workers(force=True)
        publish.assert_not_called()
        self.assertEqual(
            lifecycle.pending_workers["builder"]["phase"], "prepared"
        )

    def test_recovery_uses_actual_worker_fact_before_publishing(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        lifecycle.worker_status_resolver = (
            lambda _worker_id, _contact_id: (
                "Worker 'builder' (terminal, codex) status: running"
            )
        )
        reference = lifecycle.journal_worker_start(
            "builder", "terminal", "Build the release"
        )
        with mock.patch.object(
            long_task_updates,
            "record_worker_started",
            return_value=reference,
        ) as publish:
            lifecycle.replay_pending_once(recovery=True)

        self.assertNotIn("builder", lifecycle.pending_workers)
        self.assertEqual(publish.call_args.kwargs["state_name"], "in_progress")

    def test_recovery_removes_prepared_intent_when_worker_never_started(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        lifecycle.worker_status_resolver = (
            lambda _worker_id, _contact_id: "Error: Worker 'builder' not found."
        )
        lifecycle.journal_worker_start(
            "builder", "terminal", "Build the release"
        )
        lifecycle.replay_pending_once(recovery=True)
        self.assertNotIn("builder", lifecycle.pending_workers)

    def test_terminal_worker_fact_racing_card_create_is_not_overwritten(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        reference = lifecycle.journal_worker_start(
            "builder", "terminal", "Build the release"
        )
        with lifecycle._lock:
            intent = lifecycle.pending_workers["builder"]
            intent["phase"] = "launched"
            intent["state"] = "in_progress"
            intent["state_description"] = "Worker is running"
            intent["fact_updated_at"] = time.time()
            lifecycle._persist(active=True)

        def create_card(*_args, **_kwargs):
            # Simulate a worker process completing after the create payload was
            # captured but before its response was journaled locally.
            self.assertTrue(
                long_task_updates.record_pending_worker_state(
                    "carbon-a",
                    "builder",
                    "completed",
                    "Build delivered",
                )
            )
            return reference

        with (
            mock.patch.object(
                long_task_updates,
                "record_worker_started",
                side_effect=create_card,
            ),
            mock.patch.object(
                long_task_updates,
                "record_worker_state",
                return_value=True,
            ) as update,
        ):
            self.assertEqual(
                lifecycle._deliver_pending_workers(force=True),
                {},
            )
            pending = lifecycle.pending_workers["builder"]
            self.assertEqual(pending["phase"], "published")
            self.assertEqual(pending["state"], "completed")
            delivered = lifecycle._deliver_pending_workers(force=True)

        self.assertEqual(delivered["builder"], reference)
        self.assertNotIn("builder", lifecycle.pending_workers)
        update.assert_called_once_with(
            "carbon-a",
            "builder",
            "completed",
            "Build delivered",
        )

    def test_newer_worker_fact_racing_state_update_is_replayed_next(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        reference = lifecycle.journal_worker_start(
            "builder", "terminal", "Build the release"
        )
        with lifecycle._lock:
            intent = lifecycle.pending_workers["builder"]
            intent["phase"] = "published"
            intent["state"] = "in_progress"
            intent["state_description"] = "Worker is running"
            intent["published_reference"] = {}
            intent["fact_updated_at"] = time.time()
            lifecycle._persist(active=True)

        call_count = 0

        def update_card(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                self.assertTrue(
                    long_task_updates.record_pending_worker_state(
                        "carbon-a",
                        "builder",
                        "completed",
                        "Build delivered",
                    )
                )
            return True

        with mock.patch.object(
            long_task_updates,
            "record_worker_state",
            side_effect=update_card,
        ) as update:
            self.assertEqual(
                lifecycle._deliver_pending_workers(force=True),
                {},
            )
            pending = lifecycle.pending_workers["builder"]
            self.assertEqual(pending["state"], "completed")
            delivered = lifecycle._deliver_pending_workers(force=True)

        self.assertEqual(delivered["builder"], reference)
        self.assertNotIn("builder", lifecycle.pending_workers)
        self.assertEqual(update.call_count, 2)
        self.assertEqual(update.call_args_list[1].args[2], "completed")

    def test_slow_worker_start_cannot_race_the_watchdog_card(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        entered = threading.Event()
        release = threading.Event()

        def slow_start(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            return "Done. started"

        with (
            mock.patch.object(main, "current_long_task", return_value=lifecycle),
            mock.patch.object(main, "start_worker", side_effect=slow_start),
            mock.patch.object(main, "send_progress"),
            mock.patch.object(
                long_task_updates,
                "record_worker_started",
                return_value={},
            ) as publish,
        ):
            thread = threading.Thread(
                target=main._execute_single_tool,
                args=(
                    {
                        "tool": "worker/terminal",
                        "type": "new",
                        "worker-id": "builder",
                        "task": "Build it",
                    },
                    "carbon-a",
                ),
            )
            thread.start()
            self.assertTrue(entered.wait(1))
            lifecycle._deliver_pending_workers(force=True)
            publish.assert_not_called()
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_worker_start_failure_removes_prepared_intent(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        with (
            mock.patch.object(main, "current_long_task", return_value=lifecycle),
            mock.patch.object(
                main, "start_worker", return_value="Error: provider unavailable"
            ),
            mock.patch.object(main, "send_progress"),
        ):
            main._execute_single_tool(
                {
                    "tool": "worker/terminal",
                    "type": "new",
                    "worker-id": "builder",
                    "task": "Build it",
                },
                "carbon-a",
            )
        self.assertNotIn("builder", lifecycle.pending_workers)

    def test_worker_is_not_started_when_durable_admission_is_full(self):
        lifecycle = mock.Mock()
        lifecycle.ensure.return_value = "task-a"
        lifecycle.journal_worker_start.return_value = {}
        with (
            mock.patch.object(main, "current_long_task", return_value=lifecycle),
            mock.patch.object(main, "start_worker") as start,
        ):
            result = main._execute_single_tool(
                {
                    "tool": "worker/terminal",
                    "type": "new",
                    "worker-id": "builder",
                    "task": "Build it",
                },
                "carbon-a",
            )

        self.assertIn("durable worker update admission is unavailable", result)
        self.assertIn("worker was not started", result)
        start.assert_not_called()

    def test_timer_intent_survives_failure_and_response_loss(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        lifecycle.defer(
            "Provider rate limited", pause_reason="rate_limited"
        )
        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "task-a",
                    "state": "running",
                    "timer_state": "running",
                },
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
                return_value=ERROR,
            ),
        ):
            self.assertFalse(lifecycle._reconcile_timer(force=True))
        self.assertTrue(lifecycle._timer_dirty)

        lifecycle._next_timer_attempt_at = 0
        with (
            mock.patch.object(
                long_task_updates,
                "refresh_task_snapshot",
                return_value={
                    "task_id": "task-a",
                    "state": "running",
                    "timer_state": "paused",
                    "timer_pause_reason": "rate_limited",
                },
            ),
            mock.patch.object(
                long_task_updates,
                "execute_work_update",
            ) as execute,
        ):
            self.assertTrue(lifecycle._reconcile_timer(force=True))
        execute.assert_not_called()
        self.assertFalse(lifecycle._timer_dirty)

    def test_blocker_timer_remains_paused_until_remote_resolution(self):
        lifecycle = self.lifecycle()
        lifecycle.task_id = "task-a"
        lifecycle.task_confirmed = True
        lifecycle.defer("Need approval", pause_reason="blocker")
        lifecycle.attach("run-b", "message:\nBlue", None)
        self.assertTrue(lifecycle._deferred)
        self.assertEqual(lifecycle._desired_pause_reason, "blocker")

        lifecycle.record_work_update(
            {
                "tool": "work_update",
                "action": "blocker/resolve",
                "task_id": "task-a",
                "blocker_id": "colour",
            },
            [],
            [DONE],
        )
        with mock.patch.object(
            long_task_updates,
            "refresh_task_snapshot",
            return_value={
                "task_id": "task-a",
                "state": "running",
                "timer_state": "running",
            },
        ):
            self.assertTrue(lifecycle._reconcile_timer(force=True))
        self.assertFalse(lifecycle._deferred)
        self.assertEqual(lifecycle._desired_timer_state, "running")

    def test_direct_room_handoff_is_visible_but_internal_handoff_is_not(self):
        direct = mock.Mock(trigger="handoff", room_id="room-a")
        internal = mock.Mock(trigger="handoff", room_id="")
        self.assertTrue(main._work_lifecycle_is_visible(direct, "handoff"))
        self.assertFalse(main._work_lifecycle_is_visible(internal, "handoff"))
        self.assertTrue(
            main._work_lifecycle_is_visible(
                internal,
                "room_id: room-a\nevent_id: event-a\nmessage:\nContinue",
            )
        )

    def test_settled_state_is_content_free_and_store_is_private(self):
        lifecycle = self.lifecycle()
        sender = mock.Mock(return_value="Message sent")
        self.assertEqual(
            lifecycle.deliver_final_reply(
                "Sensitive final prose",
                has_active_workers=False,
                reply_sender=sender,
            ),
            "Message sent",
        )
        entry = long_task_updates._state_entry("carbon-a")
        self.assertFalse(entry["active"])
        self.assertNotIn("title", entry)
        self.assertNotIn("pending_reply", entry)
        self.assertNotIn("Sensitive", str(entry))
        mode = long_task_updates.LONG_TASK_STATE_FILE.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_alias_worker_and_active_contact_state_are_bounded(self):
        lifecycle = self.lifecycle()
        lifecycle.task_aliases = {
            f"old-task-{index}": "task-a"
            for index in range(long_task_updates.MAX_ALIASES + 20)
        }
        lifecycle.todo_aliases = {
            f"old-todo-{index}": "todo-a"
            for index in range(long_task_updates.MAX_ALIASES + 20)
        }
        lifecycle._persist(active=True)
        entry = long_task_updates._state_entry("carbon-a")
        self.assertLessEqual(
            len(entry["task_aliases"]), long_task_updates.MAX_ALIASES
        )
        self.assertLessEqual(
            len(entry["todo_aliases"]), long_task_updates.MAX_ALIASES
        )

        def fill(state):
            contacts = state["contacts"]
            for index in range(long_task_updates.MAX_ACTIVE_CONTACTS):
                contacts[f"contact-{index}"] = {
                    "active": True,
                    "run_id": f"run-{index}",
                    "updated_at": time.time(),
                    "lease_owner": f"owner-{index}",
                    "lease_pid": os.getpid(),
                    "lease_until": time.time() + 60,
                }

        long_task_updates.update_json(
            long_task_updates.LONG_TASK_STATE_FILE,
            long_task_updates._default_state(),
            fill,
        )
        self.assertIsNone(
            long_task_updates._claim_contact(
                "over-cap",
                "owner",
                expected_run_id="run",
                allow_create=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
