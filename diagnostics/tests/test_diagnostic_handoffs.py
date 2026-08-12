import os
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from interface import messages
from diagnostics.store import Diagnostics
from manager.runtime.maintenance import MaintenanceCoordinator


class DiagnosticHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = os.path.join(self.temp.name, "manager_queue.json")
        self.queue_patch = mock.patch.object(messages, "MANAGER_MESSAGES_FILE", self.queue)
        self.queue_patch.start()

    def tearDown(self):
        Diagnostics.unregister_active("carbon-a")
        Diagnostics.consume_pending_contexts("silicon-b")
        self.queue_patch.stop()
        self.temp.cleanup()

    def test_manager_queue_carries_parent_trace_and_handoff_identity(self):
        trace = Diagnostics.start_run(
            "message",
            "carbon-a",
            base_dir=self.temp.name,
            room_id="room-root",
            message_ids=["evt-root"],
        )
        Diagnostics.register_active("carbon-a", trace)

        messages.send_manager_message(
            "carbon-a", "silicon-b", "please handle this", target_type="silicon"
        )
        delivered = messages.check_manager_messages()
        pending = Diagnostics.consume_pending_contexts("silicon-b")

        self.assertIn("silicon-b", delivered)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["source_run_id"], trace.run_id)
        self.assertEqual(pending[0]["message_ids"], ["evt-root"])
        self.assertEqual(pending[0]["target_type"], "silicon")
        self.assertTrue(pending[0]["handoff_id"])
        queued = [event for event in trace.events if event["name"] == "handoff.queued"]
        self.assertEqual(queued[0]["meta"]["handoff_id"], pending[0]["handoff_id"])
        trace.close()

    def test_call_bookkeeping_failure_keeps_manager_message_until_retry(self):
        work_call = {
            "owner_contact_id": "carbon-a",
            "call_id": "call-a",
            "work_event_id": "event-a",
            "target_kind": "silicon",
            "target_id": "silicon-b",
        }
        with mock.patch.object(
            messages,
            "_ensure_manager_work_calls",
            side_effect=OSError("disk unavailable"),
        ):
            messages.send_manager_message(
                "carbon-a",
                "silicon-b",
                "Please review this.",
                work_call=work_call,
            )
            self.assertEqual(messages.check_manager_messages(), {})

        queued = messages._load_manager_messages()
        self.assertEqual(
            queued["silicon-b"][0]["message"],
            "Please review this.",
        )

        with mock.patch.object(
            messages,
            "_ensure_manager_work_calls",
            return_value={"call_id": "call-inbound"},
        ):
            delivered = messages.check_manager_messages()

        self.assertIn("Please review this.", delivered["silicon-b"])
        self.assertEqual(messages._load_manager_messages(), {})

    def test_durable_root_failure_keeps_manager_source_record(self):
        messages.send_manager_message(
            "carbon-a",
            "silicon-b",
            "Please preserve this handoff.",
        )
        coordinator = mock.Mock()
        coordinator.enqueue_ingress_root.side_effect = OSError(
            "state unavailable"
        )

        with mock.patch(
            "manager.runtime.maintenance.COORDINATOR",
            coordinator,
        ):
            self.assertEqual(messages.check_manager_messages_durable(), {})

        queued = messages._load_manager_messages()
        self.assertEqual(
            queued["silicon-b"][0]["message"],
            "Please preserve this handoff.",
        )

    def test_lost_ingress_acceptance_response_does_not_duplicate_turn(self):
        coordinator = MaintenanceCoordinator(
            self.temp.name,
            state_file=Path(self.temp.name) / "maintenance.json",
        )
        original_enqueue = coordinator.enqueue_ingress_root

        def commit_then_lose_response(*args, **kwargs):
            original_enqueue(*args, **kwargs)
            raise OSError("response lost")

        messages.send_manager_message(
            "carbon-a",
            "silicon-b",
            "Please run this exactly once.",
        )
        with (
            mock.patch(
                "manager.runtime.maintenance.COORDINATOR",
                coordinator,
            ),
            mock.patch.object(
                coordinator,
                "enqueue_ingress_root",
                side_effect=commit_then_lose_response,
            ),
        ):
            self.assertEqual(messages.check_manager_messages_durable(), {})

        self.assertIn("silicon-b", messages._load_manager_messages())
        first_turn = coordinator.claim_pending_roots()
        self.assertEqual(len(first_turn), 1)
        self.assertIn("Please run this exactly once.", first_turn[0].context)
        coordinator.complete_roots(first_turn)

        with mock.patch(
            "manager.runtime.maintenance.COORDINATOR",
            coordinator,
        ):
            self.assertEqual(messages.check_manager_messages_durable(), {})

        self.assertEqual(messages._load_manager_messages(), {})
        self.assertEqual(coordinator.claim_pending_roots(), [])

    def test_manager_queue_capacity_rejects_unique_item_but_accepts_replay(self):
        first = {
            "queue_id": "queue-1",
            "from_contact_id": "carbon-a",
            "message": "first private body",
        }
        second = {
            "queue_id": "queue-2",
            "from_contact_id": "carbon-a",
            "message": "second private body",
        }
        with mock.patch.object(messages, "MANAGER_QUEUE_MAX_ITEMS", 2):
            messages._append_manager_queue_item("silicon-b", first)
            messages._append_manager_queue_item("silicon-b", second)
            messages._append_manager_queue_item("silicon-b", dict(first))
            with self.assertRaises(messages.ManagerQueueConflictError):
                messages._append_manager_queue_item(
                    "silicon-c",
                    {**first, "message": "conflicting replay is rejected"},
                )
            with self.assertRaises(messages.ManagerQueueCapacityError):
                messages._append_manager_queue_item(
                    "silicon-b",
                    {
                        "queue_id": "queue-3",
                        "from_contact_id": "carbon-a",
                        "message": "must not be accepted",
                    },
                )
            health = messages.manager_queue_health()

        state = messages._load_manager_messages()
        self.assertEqual(len(state["silicon-b"]), 2)
        self.assertNotIn("silicon-c", state)
        self.assertNotIn("queue-3", json.dumps(state))
        self.assertEqual(
            health,
            {
                "queued": 2,
                "capacity": 2,
                "overflow_count": 1,
                "last_overflow_at": health["last_overflow_at"],
            },
        )
        self.assertGreater(health["last_overflow_at"], 0)
        self.assertNotIn(
            "first private body",
            json.dumps(health),
        )

    def test_manager_queue_capacity_is_atomic_under_concurrent_senders(self):
        accepted = []
        rejected = []
        result_lock = threading.Lock()

        def append(index):
            try:
                messages._append_manager_queue_item(
                    f"silicon-{index % 3}",
                    {
                        "queue_id": f"queue-{index}",
                        "from_contact_id": "carbon-a",
                        "message": f"private body {index}",
                    },
                )
            except messages.ManagerQueueCapacityError:
                with result_lock:
                    rejected.append(index)
            else:
                with result_lock:
                    accepted.append(index)

        with mock.patch.object(messages, "MANAGER_QUEUE_MAX_ITEMS", 8):
            threads = [
                threading.Thread(target=append, args=(index,))
                for index in range(32)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            health = messages.manager_queue_health()

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(accepted), 8)
        self.assertEqual(len(rejected), 24)
        self.assertEqual(health["queued"], 8)
        self.assertEqual(health["overflow_count"], 24)
        state = messages._load_manager_messages()
        self.assertEqual(
            sum(
                len(values)
                for key, values in state.items()
                if key != messages.MANAGER_QUEUE_META_KEY
            ),
            8,
        )

    def test_manager_call_replays_use_the_same_queue_idempotency_keys(self):
        item = {
            "queue_id": "queue-1",
            "from_contact_id": "carbon-a",
            "message": "Please review this.",
            "work_call": {
                "owner_contact_id": "carbon-a",
                "call_id": "call-a",
                "target_kind": "silicon",
                "target_id": "silicon-b",
            },
        }
        with (
            mock.patch(
                "interface.get_contact",
                side_effect=lambda value: {
                    "contact_type": (
                        "silicon" if value == "silicon-b" else "carbon"
                    ),
                    "display_name": value,
                },
            ),
            mock.patch(
                "interface.work.enqueue_outbound_call",
                return_value=True,
            ) as outbound,
            mock.patch(
                "interface.work.enqueue_inbound_call",
                return_value={"call_id": "call-inbound"},
            ) as inbound,
        ):
            messages._ensure_manager_work_calls("silicon-b", item)
            messages._ensure_manager_work_calls("silicon-b", item)

        self.assertEqual(outbound.call_count, 2)
        self.assertEqual(inbound.call_count, 2)
        self.assertEqual(
            {
                call.kwargs["idempotency_key"]
                for call in outbound.call_args_list
            },
            {"manager-handoff:queue-1:outbound"},
        )
        self.assertEqual(
            {
                call.kwargs["idempotency_key"]
                for call in inbound.call_args_list
            },
            {"manager-handoff:queue-1:inbound"},
        )

    def test_response_records_glass_delivery_boundary(self):
        trace = Diagnostics.start_run(
            "message", "carbon-a", base_dir=self.temp.name
        )
        trace.add_response(
            "evt-final",
            recipient_type="carbon",
            recipient_id="carbon-a",
            accepted_by="glass",
        )
        rollup = trace.close()
        event = next(item for item in rollup["events"] if item["name"] == "message.egress")
        self.assertEqual(event["meta"]["recipient_type"], "carbon")
        self.assertEqual(event["meta"]["accepted_by"], "glass")

    def test_concurrent_send_survives_queue_delivery_transaction(self):
        formatting_started = threading.Event()
        release_formatting = threading.Event()
        send_finished = threading.Event()
        delivered = {}

        def slow_record(*_args, **_kwargs):
            if threading.current_thread().name == "manager-delivery":
                formatting_started.set()
                release_formatting.wait(2)
            return {}

        def deliver():
            delivered.update(messages.check_manager_messages())

        def send_next():
            messages.send_manager_message("carbon-c", "silicon-b", "second")
            send_finished.set()

        with mock.patch.object(
            messages,
            "_ensure_manager_work_calls",
            side_effect=slow_record,
        ):
            messages.send_manager_message(
                "carbon-a",
                "silicon-b",
                "first",
                work_call={"task_id": "task-1", "call_id": "call-1"},
            )
            delivery_thread = threading.Thread(
                target=deliver,
                name="manager-delivery",
            )
            delivery_thread.start()
            self.assertTrue(formatting_started.wait(2))
            with open(self.queue, encoding="utf-8") as handle:
                durable_during_formatting = json.load(handle)
            self.assertEqual(
                durable_during_formatting["silicon-b"][0]["message"],
                "first",
            )

            sender_thread = threading.Thread(target=send_next)
            sender_thread.start()
            self.assertTrue(send_finished.wait(0.2))

            release_formatting.set()
            delivery_thread.join(2)
            sender_thread.join(2)

        self.assertTrue(send_finished.is_set())
        self.assertIn("first", delivered["silicon-b"])
        next_delivery = messages.check_manager_messages()
        self.assertIn("second", next_delivery["silicon-b"])

    def test_lineage_continuation_does_not_create_a_second_inbound_card(self):
        activity = mock.Mock()
        activity.reference.return_value = {"activity_id": "lineage-a"}
        coordinator = mock.Mock()
        coordinator.acquire_activity.return_value = activity
        coordinator.enqueue_continuation.return_value = True
        continuation = {
            "owner_contact_id": "silicon-b",
            "task_id": "",
            "call_id": "call-b",
            "work_event_id": "event-b",
            "continuation": True,
        }

        with (
            mock.patch(
                "manager.runtime.maintenance.current_activity",
                return_value=object(),
            ),
            mock.patch("manager.runtime.maintenance.COORDINATOR", coordinator),
            mock.patch("helpers.process.submit_best_effort") as submit,
        ):
            accepted = messages._queue_lineage_handoff(
                "silicon-b",
                "carbon-a",
                "Here is the answer.",
                {},
                continuation,
            )

        self.assertTrue(accepted)
        submit.assert_not_called()
        queued_context = coordinator.enqueue_continuation.call_args.args[1]
        self.assertIn('"outbound_call_id": "call-b"', queued_context)

    def test_new_lineage_call_journals_cards_before_reconciliation_ack(self):
        activity = mock.Mock()
        activity.reference.return_value = {"activity_id": "lineage-a"}
        coordinator = mock.Mock()
        coordinator.acquire_activity.return_value = activity
        coordinator.enqueue_continuation.return_value = True
        work_call = {
            "owner_contact_id": "carbon-a",
            "task_id": "",
            "call_id": "call-a",
            "work_event_id": "event-a",
        }

        with (
            mock.patch(
                "manager.runtime.maintenance.current_activity",
                return_value=object(),
            ),
            mock.patch("manager.runtime.maintenance.COORDINATOR", coordinator),
            mock.patch.object(
                messages,
                "_ensure_manager_work_calls",
                return_value={"call_id": "call-inbound"},
            ) as ensure,
        ):
            accepted = messages._queue_lineage_handoff(
                "carbon-a",
                "silicon-b",
                "Please take a look.",
                {},
                work_call,
            )

        self.assertTrue(accepted)
        ensure.assert_called_once()
        self.assertEqual(messages._load_manager_messages(), {})

    def test_lost_lineage_acceptance_response_recovers_without_duplicate_turn(self):
        coordinator = MaintenanceCoordinator(
            self.temp.name,
            state_file=Path(self.temp.name) / "maintenance.json",
        )
        original_enqueue = coordinator.enqueue_continuation

        def commit_then_lose_response(*args, **kwargs):
            original_enqueue(*args, **kwargs)
            raise OSError("response lost")

        work_call = {
            "owner_contact_id": "carbon-a",
            "call_id": "call-a",
            "work_event_id": "event-a",
            "target_kind": "silicon",
            "target_id": "silicon-b",
        }
        with (
            mock.patch(
                "manager.runtime.maintenance.current_activity",
                return_value=object(),
            ),
            mock.patch(
                "manager.runtime.maintenance.COORDINATOR",
                coordinator,
            ),
            mock.patch.object(
                coordinator,
                "enqueue_continuation",
                side_effect=commit_then_lose_response,
            ),
        ):
            result = messages.send_manager_message(
                "carbon-a",
                "silicon-b",
                "Please review this.",
                target_type="silicon",
                work_call=work_call,
            )

        self.assertIn("immediate delivery", result)
        prepared = messages._load_manager_messages()["silicon-b"][0]
        self.assertTrue(prepared["lineage_prepared"])

        with (
            mock.patch(
                "manager.runtime.maintenance.COORDINATOR",
                coordinator,
            ),
            mock.patch.object(
                messages,
                "_ensure_manager_work_calls",
                return_value={"call_id": "call-inbound"},
            ),
        ):
            self.assertEqual(messages.check_manager_messages(), {})

        turns = coordinator.claim_pending_roots()
        self.assertEqual(len(turns), 1)
        self.assertIn("Please review this.", turns[0].context)
        self.assertEqual(messages._load_manager_messages(), {})
