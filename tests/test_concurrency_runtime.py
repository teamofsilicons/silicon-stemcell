import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import interface

from interface import work_updates
from helpers.process import BestEffortOutbox, flush_best_effort
from helpers.state import (
    lock_handle,
    read_json,
    unlock_handle,
    update_json,
    update_json_if_changed,
)
from worker import handler


class BackgroundOutboxTest(unittest.TestCase):
    def test_keyed_work_is_ordered_and_queued_updates_are_coalesced(self):
        outbox = BestEffortOutbox(max_pending=8, workers=2)
        started = threading.Event()
        release = threading.Event()
        observed = []

        def first():
            started.set()
            release.wait(2)
            observed.append("first")

        self.assertTrue(outbox.submit(first, key="room"))
        self.assertTrue(started.wait(1))
        self.assertTrue(
            outbox.submit(
                observed.append,
                "superseded",
                key="room",
                coalesce=True,
            )
        )
        self.assertTrue(
            outbox.submit(
                observed.append,
                "latest",
                key="room",
                coalesce=True,
            )
        )
        release.set()
        self.assertTrue(outbox.flush(2))
        outbox.close()
        self.assertEqual(observed, ["first", "latest"])


class PrimaryDeliveryLatencyTest(unittest.TestCase):
    def tearDown(self):
        flush_best_effort(2)

    def test_progress_transport_cannot_block_caller(self):
        entered = threading.Event()
        release = threading.Event()
        client = mock.Mock()

        def slow_progress(*_args, **_kwargs):
            entered.set()
            release.wait(2)

        client.progress.side_effect = slow_progress
        with (
            mock.patch.object(
                interface.outbound,
                "get_contact",
                return_value={"room_id": "room-a"},
            ),
            mock.patch.object(interface.client, "InterfaceClient", return_value=client),
        ):
            started = time.monotonic()
            interface.send_progress(
                "carbon-a",
                "manager-run:a",
                "thinking",
                "working",
                frame_id="frame-a",
                revision=0,
            )
            elapsed = time.monotonic() - started
            self.assertTrue(entered.wait(1))
            self.assertLess(elapsed, 0.1)
            release.set()
            self.assertTrue(flush_best_effort(2))

    def test_inbound_context_does_not_wait_for_bookkeeping_or_read_receipt(self):
        bookkeeping_started = threading.Event()
        read_started = threading.Event()
        release = threading.Event()

        def slow_bookkeeping(*_args, **_kwargs):
            bookkeeping_started.set()
            release.wait(2)

        class SlowClient:
            def read(self, *_args):
                read_started.set()
                release.wait(2)

        contact = {
            "contact_type": "carbon",
            "carbon_id": "carbon-a",
            "display_name": "Carbon A",
        }
        with (
            mock.patch.object(interface.ingest, "_load_state", return_value={}),
            mock.patch.object(
                interface.ingest,
                "_contact_for_room",
                return_value=("carbon-a", contact, False),
            ),
            mock.patch.object(interface.ingest, "_already_processed", return_value=False),
            mock.patch.object(interface.ingest, "_remember_processed"),
            mock.patch.object(
                interface.ingest,
                "_record_incoming_bookkeeping",
                side_effect=slow_bookkeeping,
            ),
            mock.patch(
                "diagnostics.store.Diagnostics.get_active_run",
                return_value=None,
            ),
            mock.patch(
                "diagnostics.store.Diagnostics.start_run",
                side_effect=RuntimeError,
            ),
        ):
            started = time.monotonic()
            result = interface.process_incoming_event(
                {
                    "type": "m.text",
                    "event_id": "event-a",
                    "room_id": "room-a",
                    "content": {"body": "hello"},
                },
                client=SlowClient(),
            )
            elapsed = time.monotonic() - started

        self.assertEqual(result[0], "carbon-a")
        self.assertLess(elapsed, 0.1)
        self.assertTrue(bookkeeping_started.wait(1))
        self.assertTrue(read_started.wait(1))
        release.set()


class ConcurrentJsonStateTest(unittest.TestCase):
    def test_conditional_update_skips_noop_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            update_json(path, {"count": 0}, lambda _state: None)
            previous = path.stat()

            result = update_json_if_changed(
                path,
                {"count": 0},
                lambda state: state.get("count"),
            )

            current = path.stat()
            self.assertEqual(result, 0)
            self.assertEqual(current.st_ino, previous.st_ino)
            self.assertEqual(current.st_mtime_ns, previous.st_mtime_ns)

    def test_atomic_updates_do_not_lose_parallel_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "counter.json"

            def increment():
                for _ in range(50):
                    update_json(
                        path,
                        {"count": 0},
                        lambda state: state.__setitem__(
                            "count",
                            int(state.get("count") or 0) + 1,
                        ),
                    )

            threads = [threading.Thread(target=increment) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(read_json(path, {})["count"], 200)

    def test_worker_registration_is_a_single_atomic_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = str(Path(temp) / "registry.json")
            results = []

            def create():
                results.append(
                    handler._create_worker_record(
                        "same-worker",
                        "terminal",
                        "carbon-a",
                    )
                )

            with mock.patch.object(handler, "WORKER_REGISTRY_FILE", registry):
                threads = [threading.Thread(target=create) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(2)

            successes = [record for record, error in results if record and not error]
            self.assertEqual(len(successes), 1)
            saved = json.loads(Path(registry).read_text(encoding="utf-8"))
            self.assertEqual(list(saved), ["same-worker"])


class CorrelationAndDiagnosticsTest(unittest.TestCase):
    def test_expired_call_correlations_are_pruned(self):
        state = {
            "version": 1,
            "contacts": {
                "carbon-a": {
                    "pending_calls": {
                        "old": {
                            "outbound_owner_contact_id": "carbon-a",
                            "outbound_task_id": "task-a",
                            "outbound_call_id": "call-a",
                            "updated_at": 1,
                        },
                        "fresh": {
                            "outbound_owner_contact_id": "carbon-a",
                            "outbound_task_id": "task-a",
                            "outbound_call_id": "call-b",
                            "updated_at": 10_000,
                        },
                    },
                    "tasks": {},
                }
            },
        }
        work_updates._prune_state(
            state,
            now=10_000 + work_updates.PENDING_CALL_TTL_SECONDS - 1,
        )
        self.assertNotIn(
            "old",
            state["contacts"]["carbon-a"]["pending_calls"],
        )
        self.assertIn(
            "fresh",
            state["contacts"]["carbon-a"]["pending_calls"],
        )

    def test_event_arriving_during_manager_run_is_not_added_to_old_trace(self):
        active = mock.Mock()
        active.meta = {"_manager_running": True}
        contact = {
            "contact_type": "carbon",
            "carbon_id": "carbon-a",
            "display_name": "Carbon A",
        }
        with (
            mock.patch.object(interface.ingest, "_load_state", return_value={}),
            mock.patch.object(
                interface.ingest,
                "_contact_for_room",
                return_value=("carbon-a", contact, False),
            ),
            mock.patch.object(interface.ingest, "_already_processed", return_value=False),
            mock.patch.object(interface.ingest, "_remember_processed"),
            mock.patch("helpers.process.submit_best_effort", return_value=True),
            mock.patch(
                "diagnostics.store.Diagnostics.get_active_run",
                return_value=active,
            ),
        ):
            result = interface.process_incoming_event(
                {
                    "type": "m.text",
                    "event_id": "next-event",
                    "room_id": "room-a",
                    "content": {"body": "next turn"},
                },
                client=mock.Mock(),
            )

        self.assertEqual(result[0], "carbon-a")
        active.add_message.assert_not_called()
        active.event.assert_not_called()


class LockHandleTest(unittest.TestCase):
    def test_non_blocking_lock_reports_contention_instead_of_waiting(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "contended.lock"
            path.touch()
            with path.open("r+b") as holder:
                self.assertTrue(lock_handle(holder))
                # A second handle on the same file is a distinct flock owner.
                with path.open("r+b") as rival:
                    self.assertFalse(lock_handle(rival, blocking=False))
                unlock_handle(holder)
                with path.open("r+b") as rival:
                    self.assertTrue(lock_handle(rival, blocking=False))
                    unlock_handle(rival)


if __name__ == "__main__":
    unittest.main()
