import os
import json
import tempfile
import threading
import unittest
from unittest import mock

from core import messages
from core.diagnostics import Diagnostics


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
        messages.send_manager_message(
            "carbon-a",
            "silicon-b",
            "first",
            work_call={"task_id": "task-1"},
        )
        formatting_started = threading.Event()
        release_formatting = threading.Event()
        send_finished = threading.Event()
        delivered = {}

        def slow_record(*_args, **_kwargs):
            formatting_started.set()
            release_formatting.wait(2)
            return {}

        def deliver():
            delivered.update(messages.check_manager_messages())

        def send_next():
            messages.send_manager_message("carbon-c", "silicon-b", "second")
            send_finished.set()

        with (
            mock.patch("core.interface.get_contact", return_value={}),
            mock.patch("core.work_updates.enqueue_inbound_call", side_effect=slow_record),
        ):
            delivery_thread = threading.Thread(target=deliver)
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
