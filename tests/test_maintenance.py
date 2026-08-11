import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from manager.runtime.maintenance import (
    IngressRootConflictError,
    LEASE_TTL_SECONDS,
    MaintenanceCoordinator,
    bind_activity,
)


def _enqueue_in_process(state_file, prefix, count):
    coordinator = MaintenanceCoordinator(
        Path(state_file).parents[2],
        state_file=state_file,
    )
    for index in range(count):
        coordinator.enqueue_root(
            f"{prefix}-{index}",
            f"context-{prefix}-{index}",
        )


class FakeClock:
    def __init__(self, value=1_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class MaintenanceCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = FakeClock()
        self.coordinator = MaintenanceCoordinator(
            self.root,
            state_file=self.root / "state.json",
            clock=self.clock,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_status_read_does_not_rewrite_unchanged_state(self):
        self.coordinator.request_drain(deadline_seconds=60)

        with mock.patch("manager.runtime.maintenance.store.write_json") as write:
            status = self.coordinator.public_status()

        self.assertEqual(status["phase"], "draining")
        write.assert_not_called()

    def test_failed_root_admission_retries_with_bounded_backoff(self):
        result = self.coordinator.enqueue_root("carbon-a", "active task")
        self.assertIsNotNone(result.admission)

        expected_delays = [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0]
        admission = result.admission
        for attempt, expected_delay in enumerate(expected_delays, start=1):
            self.coordinator.retry_roots([admission], delay=5.0)
            state = json.loads((self.root / "state.json").read_text())
            item = state["root_queue"][0]
            self.assertEqual(item["attempts"], attempt)
            self.assertEqual(
                item["not_before"],
                self.clock.value + expected_delay,
            )
            self.clock.advance(expected_delay)
            claimed = self.coordinator.claim_pending_roots()
            self.assertEqual(len(claimed), 1)
            admission = claimed[0]

    def test_active_lineage_finishes_while_new_root_is_durably_queued(self):
        first = self.coordinator.enqueue_root("carbon-a", "active task")
        self.assertIsNotNone(first.admission)
        root = first.admission.activity

        status = self.coordinator.request_drain(deadline_seconds=60)
        self.assertEqual(status["phase"], "draining")
        self.assertEqual(status["active_count"], 1)

        with bind_activity(root):
            worker = self.coordinator.acquire_activity(
                "worker",
                activity_id="browser-1",
                contact_id="carbon-a",
            )
        self.assertIsNotNone(worker)

        later = self.coordinator.enqueue_root(
            "carbon-b",
            "SECRET: must stay local and run after update",
        )
        self.assertIsNone(later.admission)
        self.assertTrue(later.queued_for_maintenance)

        self.coordinator.complete_roots([first.admission])
        status = self.coordinator.public_status()
        self.assertEqual(status["active_by_kind"], {"worker": 1})
        self.assertFalse(status["safe_to_stop"])

        self.coordinator.release(worker)
        self.assertTrue(
            self.coordinator.acknowledge_runtime_quiescent(
                epoch=status["epoch"],
                outbox_flushed=True,
            )
        )
        status = self.coordinator.public_status()
        self.assertTrue(status["safe_to_stop"])
        self.assertEqual(status["queued_message_count"], 1)
        self.assertNotIn("SECRET", json.dumps(status))
        self.assertNotIn("SECRET", json.dumps(self.coordinator.public_events()))

    def test_cancel_releases_queued_root_for_exact_replay(self):
        status = self.coordinator.request_drain()
        queued = self.coordinator.enqueue_root("carbon-a", "queued once")
        self.assertTrue(queued.queued_for_maintenance)
        self.assertTrue(self.coordinator.cancel_drain(status["maintenance_id"]))

        claimed = self.coordinator.claim_pending_roots()
        self.assertEqual([item.context for item in claimed], ["queued once"])
        self.assertEqual(self.coordinator.claim_pending_roots(), [])
        self.coordinator.complete_roots(claimed)
        self.assertEqual(
            self.coordinator.public_status()["queued_message_count"],
            0,
        )

    def test_prefence_worker_completion_runs_as_a_draining_continuation(self):
        first = self.coordinator.enqueue_root("carbon-a", "start worker").admission
        with bind_activity(first.activity):
            worker = self.coordinator.acquire_activity(
                "worker",
                activity_id="worker-a",
                contact_id="carbon-a",
            )
        drain = self.coordinator.request_drain()
        self.coordinator.complete_roots([first])

        self.assertTrue(
            self.coordinator.enqueue_continuation(
                "carbon-a",
                "worker completed",
                worker.reference(),
            )
        )
        status = self.coordinator.public_status()
        self.assertEqual(status["active_by_kind"], {"continuation_pending": 1})
        self.assertFalse(
            self.coordinator.acknowledge_runtime_quiescent(
                epoch=drain["epoch"],
                outbox_flushed=True,
            )
        )

        continuation = self.coordinator.claim_pending_roots()
        self.assertEqual([item.context for item in continuation], ["worker completed"])
        self.assertEqual(
            self.coordinator.public_status()["active_by_kind"],
            {"manager_root": 1},
        )
        self.coordinator.complete_roots(continuation)
        self.assertTrue(
            self.coordinator.acknowledge_runtime_quiescent(
                epoch=drain["epoch"],
                outbox_flushed=True,
            )
        )

    def test_continuation_queue_id_is_idempotent_after_completion(self):
        root = self.coordinator.enqueue_root(
            "carbon-a",
            "source",
        ).admission
        descendant = self.coordinator.acquire_activity(
            "manager_handoff",
            activity_id="handoff-a",
            contact_id="carbon-b",
            parent=root.activity,
        )
        self.assertIsNotNone(descendant)

        self.assertTrue(
            self.coordinator.enqueue_continuation(
                "carbon-b",
                "same continuation",
                descendant.reference(),
                queue_id="handoff-queue-a",
            )
        )
        self.assertTrue(
            self.coordinator.enqueue_continuation(
                "carbon-b",
                "same continuation",
                descendant.reference(),
                queue_id="handoff-queue-a",
            )
        )
        claimed = self.coordinator.claim_pending_roots()
        self.assertEqual(len(claimed), 1)
        self.coordinator.complete_roots(claimed)

        self.assertTrue(
            self.coordinator.enqueue_continuation(
                "carbon-b",
                "same continuation",
                descendant.reference(),
                queue_id="handoff-queue-a",
            )
        )
        self.assertEqual(self.coordinator.claim_pending_roots(), [])
        self.assertFalse(
            self.coordinator.enqueue_continuation(
                "carbon-b",
                "different continuation",
                descendant.reference(),
                queue_id="handoff-queue-a",
            )
        )

    def test_ingress_root_is_pending_and_idempotent_after_completion(self):
        private_context = "SECRET manager context"
        self.assertTrue(
            self.coordinator.enqueue_ingress_root(
                "carbon-a",
                private_context,
                ingress_id="interface:room-a:event-a",
            )
        )
        status = self.coordinator.public_status()
        self.assertEqual(status["active_count"], 0)
        self.assertEqual(status["queued_message_count"], 1)

        self.assertTrue(
            self.coordinator.enqueue_ingress_root(
                "carbon-a",
                private_context,
                ingress_id="interface:room-a:event-a",
            )
        )
        claimed = self.coordinator.claim_pending_roots()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].context, private_context)
        self.coordinator.complete_roots(claimed)

        self.assertTrue(
            self.coordinator.enqueue_ingress_root(
                "carbon-a",
                private_context,
                ingress_id="interface:room-a:event-a",
            )
        )
        self.assertEqual(self.coordinator.claim_pending_roots(), [])
        raw = json.loads(
            self.coordinator.state_file.read_text(encoding="utf-8")
        )
        self.assertNotIn(private_context, json.dumps(raw["ingress_receipts"]))

    def test_ingress_identity_conflict_is_body_free(self):
        self.coordinator.enqueue_ingress_root(
            "carbon-a",
            "first private context",
            ingress_id="manager:queue-a",
        )

        with self.assertRaises(IngressRootConflictError) as raised:
            self.coordinator.enqueue_ingress_root(
                "carbon-a",
                "second private context",
                ingress_id="manager:queue-a",
            )

        self.assertNotIn("first private", str(raised.exception))
        self.assertNotIn("second private", str(raised.exception))
        claimed = self.coordinator.claim_pending_roots()
        self.assertEqual(
            [item.context for item in claimed],
            ["first private context"],
        )

    def test_deadline_cancels_drain_without_terminating_active_work(self):
        admitted = self.coordinator.enqueue_root("carbon-a", "long task").admission
        self.coordinator.request_drain(deadline_seconds=5)
        self.clock.advance(6)

        status = self.coordinator.public_status()
        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["last_outcome"], "deadline_expired")
        self.assertEqual(status["active_count"], 1)

        self.coordinator.complete_roots([admitted])

    def test_deadline_cancels_even_after_runtime_acknowledges_quiescence(self):
        status = self.coordinator.request_drain(
            deadline_seconds=5,
            maintenance_id="update-a",
        )
        self.assertTrue(
            self.coordinator.acknowledge_runtime_quiescent(
                epoch=status["epoch"],
                outbox_flushed=True,
            )
        )
        self.assertTrue(self.coordinator.public_status()["safe_to_stop"])

        self.clock.advance(6)
        expired = self.coordinator.public_status()
        self.assertEqual(expired["phase"], "available")
        self.assertFalse(expired["safe_to_stop"])
        self.assertEqual(expired["last_outcome"], "deadline_expired")

    def test_same_drain_id_is_idempotent_but_different_id_conflicts(self):
        first = self.coordinator.request_drain(
            deadline_seconds=30,
            maintenance_id="update-a",
        )
        repeated = self.coordinator.request_drain(
            deadline_seconds=120,
            maintenance_id="update-a",
        )
        self.assertEqual(repeated["maintenance_id"], "update-a")
        self.assertEqual(repeated["epoch"], first["epoch"])
        self.assertEqual(
            [event["event"] for event in self.coordinator.public_events()].count(
                "maintenance.requested"
            ),
            1,
        )

        with self.assertRaisesRegex(RuntimeError, "different"):
            self.coordinator.request_drain(maintenance_id="update-b")
        current = self.coordinator.public_status()
        self.assertEqual(current["maintenance_id"], "update-a")
        self.assertEqual(current["epoch"], first["epoch"])

    def test_stale_lease_is_pruned_before_safe_to_stop(self):
        admitted = self.coordinator.enqueue_root("carbon-a", "abandoned").admission
        status = self.coordinator.request_drain()
        self.clock.advance(LEASE_TTL_SECONDS + 1)

        status = self.coordinator.public_status()
        self.assertEqual(status["active_count"], 0)
        self.assertFalse(status["safe_to_stop"])
        self.assertTrue(
            self.coordinator.acknowledge_runtime_quiescent(
                epoch=status["epoch"],
                outbox_flushed=True,
            )
        )
        self.assertTrue(self.coordinator.public_status()["safe_to_stop"])
        # A stale claimant cannot remove a later replay of the same root.
        self.coordinator.complete_roots([admitted])
        self.assertEqual(self.coordinator.public_status()["queued_message_count"], 1)

    def test_update_transition_requires_quiescent_runtime_ack(self):
        status = self.coordinator.request_drain()
        with self.assertRaises(RuntimeError):
            self.coordinator.transition("updating", status["maintenance_id"])

        self.assertTrue(
            self.coordinator.acknowledge_runtime_quiescent(
                epoch=status["epoch"],
                outbox_flushed=True,
            )
        )
        self.assertEqual(
            self.coordinator.transition(
                "updating",
                status["maintenance_id"],
            )["phase"],
            "updating",
        )

    def test_notice_is_deduplicated_and_contains_no_task_data(self):
        self.coordinator.request_drain()
        self.coordinator.enqueue_root("carbon-a", "private task one")
        self.coordinator.enqueue_root("carbon-a", "private task two")
        notices = self.coordinator.claim_notices()

        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["contact_id"], "carbon-a")
        self.assertNotIn("private task", json.dumps(notices))
        self.assertIn("safely queued", notices[0]["message"])

    def test_request_and_root_admission_race_never_loses_the_root(self):
        barrier = threading.Barrier(2)
        result = {}

        def enqueue():
            barrier.wait()
            result["enqueue"] = self.coordinator.enqueue_root(
                "carbon-race",
                "race context",
            )

        def drain():
            barrier.wait()
            result["status"] = self.coordinator.request_drain()

        threads = [threading.Thread(target=enqueue), threading.Thread(target=drain)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        enqueue_result = result["enqueue"]
        status = self.coordinator.public_status()
        if enqueue_result.admission is None:
            self.assertEqual(status["queued_message_count"], 1)
            self.assertEqual(status["active_count"], 0)
        else:
            self.assertEqual(status["queued_message_count"], 0)
            self.assertEqual(status["active_count"], 1)

    @unittest.skipIf(os.name == "nt", "fork-based lock test")
    def test_cross_process_root_admission_is_atomic(self):
        state_file = self.root / "cross-process.json"
        ctx = multiprocessing.get_context("fork")
        processes = [
            ctx.Process(
                target=_enqueue_in_process,
                args=(str(state_file), f"p{index}", 10),
            )
            for index in range(4)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)

        coordinator = MaintenanceCoordinator(
            self.root,
            state_file=state_file,
        )
        status = coordinator.public_status()
        self.assertEqual(status["active_count"], 40)
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(len(raw["root_queue"]), 40)
        self.assertEqual(len({item["queue_id"] for item in raw["root_queue"]}), 40)

    def test_generation_code_uses_silicon_data_root_for_state(self):
        data_root = self.root / "instance-data"
        data_root.mkdir()
        env = os.environ.copy()
        env["SILICON_DATA_ROOT"] = str(data_root)
        script = (
            "from manager.runtime.maintenance import COORDINATOR;"
            "COORDINATOR.request_drain(maintenance_id='generation-test');"
            "print(COORDINATOR.state_file)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        expected = (
            data_root.resolve()
            / "interface"
            / "state"
            / "maintenance.json"
        )
        self.assertEqual(Path(completed.stdout.strip()), expected)
        self.assertTrue(expected.is_file())


class DispatcherMaintenanceTests(unittest.TestCase):
    def test_active_dispatch_finishes_and_later_message_waits(self):
        import main

        with tempfile.TemporaryDirectory() as temp:
            coordinator = MaintenanceCoordinator(
                temp,
                state_file=Path(temp) / "maintenance.json",
            )
            started = threading.Event()
            finish = threading.Event()
            calls = []

            def runner(payload):
                calls.append(payload)
                if len(calls) == 1:
                    started.set()
                    self.assertTrue(finish.wait(5))

            dispatcher = main.ManagerDispatcher(runner=runner)
            with mock.patch.object(main, "MAINTENANCE", coordinator):
                dispatcher.submit({"carbon-a": "first"})
                self.assertTrue(started.wait(5))
                drain = coordinator.request_drain()
                dispatcher.submit({"carbon-b": "second"})

                status = coordinator.public_status()
                self.assertEqual(status["active_count"], 1)
                self.assertEqual(status["queued_message_count"], 1)
                self.assertEqual(len(calls), 1)

                finish.set()
                self.assertTrue(dispatcher.wait_for_idle(5))
                self.assertTrue(
                    coordinator.acknowledge_runtime_quiescent(
                        epoch=drain["epoch"],
                        outbox_flushed=True,
                    )
                )
                coordinator.cancel_drain(drain["maintenance_id"])
                self.assertEqual(dispatcher.replay_maintenance_queue(), 1)
                self.assertTrue(dispatcher.wait_for_idle(5))
                self.assertEqual(
                    calls,
                    [
                        {"carbon-a": "first"},
                        {"carbon-b": "second"},
                    ],
                )
            dispatcher.shutdown(wait=True)

    def test_runtime_ack_waits_for_inbox_claims_and_volatile_outbox(self):
        import main

        with tempfile.TemporaryDirectory() as temp:
            coordinator = MaintenanceCoordinator(
                temp,
                state_file=Path(temp) / "maintenance.json",
            )
            coordinator.request_drain()
            dispatcher = mock.Mock()
            dispatcher.wait_for_idle.return_value = True
            dispatcher.replay_maintenance_queue.return_value = 0

            with mock.patch.object(
                main,
                "MAINTENANCE",
                coordinator,
            ), mock.patch.object(
                main,
                "reconcile_maintenance_activities",
            ), mock.patch.object(
                main,
                "start_listener",
            ), mock.patch.object(
                main,
                "stop_listener",
            ), mock.patch.object(
                main,
                "schedule_maintenance_notices",
            ), mock.patch.object(
                main,
                "maintenance_inbox_quiescent",
                return_value=False,
            ), mock.patch(
                "helpers.process.flush_best_effort",
                return_value=True,
            ):
                main._maintenance_runtime_tick(dispatcher)
            self.assertFalse(coordinator.public_status()["safe_to_stop"])

            with mock.patch.object(
                main,
                "MAINTENANCE",
                coordinator,
            ), mock.patch.object(
                main,
                "reconcile_maintenance_activities",
            ), mock.patch.object(
                main,
                "start_listener",
            ), mock.patch.object(
                main,
                "stop_listener",
            ), mock.patch.object(
                main,
                "schedule_maintenance_notices",
            ), mock.patch.object(
                main,
                "maintenance_inbox_quiescent",
                return_value=True,
            ), mock.patch(
                "helpers.process.flush_best_effort",
                return_value=True,
            ):
                main._maintenance_runtime_tick(dispatcher)
            self.assertTrue(coordinator.public_status()["safe_to_stop"])


class RuntimeGatingTests(unittest.TestCase):
    def test_cron_sweep_does_not_claim_during_maintenance(self):
        from interface.cron import check_crons

        with mock.patch(
            "manager.runtime.maintenance.accepting_new_roots",
            return_value=False,
        ), mock.patch("interface.cron._check_checkbacks") as checkbacks, mock.patch(
            "interface.cron._check_glass_crons"
        ) as crons:
            self.assertEqual(check_crons(), {})
            checkbacks.assert_not_called()
            crons.assert_not_called()

    def test_new_worker_is_rejected_without_prefence_lineage(self):
        import manager.runtime.maintenance
        import worker.handler

        with tempfile.TemporaryDirectory() as temp:
            coordinator = MaintenanceCoordinator(
                temp,
                state_file=Path(temp) / "maintenance.json",
            )
            coordinator.request_drain()
            with mock.patch.object(
                manager.runtime.maintenance,
                "COORDINATOR",
                coordinator,
            ):
                result = worker.handler.start_worker(
                    "worker-during-update",
                    "should not launch",
                    "terminal",
                    "carbon-a",
                )
            self.assertIn("preparing an update", result)

    def test_legacy_active_worker_is_adopted_before_quiescence(self):
        import manager.runtime.maintenance
        import worker.handler

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            coordinator = MaintenanceCoordinator(
                root,
                state_file=root / "maintenance.json",
            )
            active_file = root / "active.json"
            queue_file = root / "browser-queue.json"
            active_file.write_text(
                json.dumps(
                    {
                        "legacy-worker": {
                            "pid": os.getpid(),
                            "started": time.time() - 10,
                            "carbon_id": "carbon-a",
                            "worker_type": "terminal",
                        }
                    }
                ),
                encoding="utf-8",
            )
            queue_file.write_text("[]", encoding="utf-8")
            coordinator.request_drain()

            with mock.patch.object(
                manager.runtime.maintenance,
                "COORDINATOR",
                coordinator,
            ), mock.patch.object(
                worker.handler,
                "ACTIVE_FILE",
                str(active_file),
            ), mock.patch.object(
                worker.handler,
                "BROWSER_QUEUE_FILE",
                str(queue_file),
            ):
                self.assertEqual(
                    worker.handler.reconcile_maintenance_activities(),
                    1,
                )
            self.assertEqual(
                coordinator.public_status()["active_by_kind"],
                {"worker": 1},
            )

    def test_manager_handoff_keeps_prefence_lineage_during_drain(self):
        import manager.runtime.maintenance
        from interface import messages

        with tempfile.TemporaryDirectory() as temp:
            coordinator = MaintenanceCoordinator(
                temp,
                state_file=Path(temp) / "maintenance.json",
            )
            source = coordinator.enqueue_root(
                "carbon-a",
                "source task",
            ).admission
            drain = coordinator.request_drain()
            queue_file = Path(temp) / "manager_queue.json"
            with mock.patch.object(
                manager.runtime.maintenance,
                "COORDINATOR",
                coordinator,
            ), mock.patch.object(
                messages,
                "MANAGER_MESSAGES_FILE",
                str(queue_file),
            ), bind_activity(source.activity):
                result = messages.send_manager_message(
                    "carbon-a",
                    "silicon-b",
                    "continue accepted task",
                )
            self.assertIn("immediate delivery", result)
            coordinator.complete_roots([source])

            handoff = coordinator.claim_pending_roots()
            self.assertEqual(len(handoff), 1)
            self.assertIn("continue accepted task", handoff[0].context)
            self.assertFalse(
                coordinator.acknowledge_runtime_quiescent(
                    epoch=drain["epoch"],
                    outbox_flushed=True,
                )
            )
            coordinator.complete_roots(handoff)
            self.assertTrue(
                coordinator.acknowledge_runtime_quiescent(
                    epoch=drain["epoch"],
                    outbox_flushed=True,
                )
            )


if __name__ == "__main__":
    unittest.main()


class InterruptedDrainUnwindTests(unittest.TestCase):
    """An update abandoned during drain must be able to unwind on its own.

    A transaction interrupted before the stop boundary never leaves "draining",
    but the recovery path asks for "rolling_back" regardless. Without that edge
    the request was refused and the transaction stayed interrupted, so every
    later update failed preflight with "cannot preflight over an interrupted
    update" until a human walked the state machine by hand. Five silicons on one
    host wedged this way.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.coordinator = MaintenanceCoordinator(self.root)

    def _drain(self):
        status = self.coordinator.request_drain()
        return status["maintenance_id"]

    def test_a_draining_update_can_roll_back(self):
        mid = self._drain()
        self.assertEqual(self.coordinator.public_status()["phase"], "draining")

        status = self.coordinator.transition("rolling_back", maintenance_id=mid)
        self.assertEqual(status["phase"], "rolling_back")

        status = self.coordinator.transition("available", maintenance_id=mid)
        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["last_outcome"], "rolled_back")

    def test_abandoning_a_drain_is_not_reported_as_updated(self):
        mid = self._drain()
        status = self.coordinator.transition("available", maintenance_id=mid)
        self.assertEqual(status["phase"], "available")
        self.assertEqual(
            status["last_outcome"],
            "cancelled",
            "a Silicon still on its old version must not report 'updated'",
        )

    def test_a_completed_update_still_reports_updated(self):
        mid = self._drain()
        state_path = self.root / "interface" / "state" / "maintenance.json"
        data = json.loads(state_path.read_text())
        data["safe_to_stop"] = True
        state_path.write_text(json.dumps(data))

        self.coordinator.transition("updating", maintenance_id=mid)
        self.coordinator.transition("validating", maintenance_id=mid)
        status = self.coordinator.transition("available", maintenance_id=mid)
        self.assertEqual(status["last_outcome"], "updated")
