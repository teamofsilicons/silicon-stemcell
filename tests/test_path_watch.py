import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import watch as path_watch


class PathChangeWaiterTest(unittest.TestCase):
    def test_inotify_backend_keeps_the_native_wait_contract(self):
        self.assertIn("wait", path_watch._InotifyBackend.__dict__)
        self.assertIn("close", path_watch._InotifyBackend.__dict__)
        backend = object.__new__(path_watch._InotifyBackend)
        backend._fd = -1
        backend._targets = {}
        backend.close()

    def test_atomic_replace_wakes_waiter(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text("before", encoding="utf-8")
            result = []
            with path_watch.PathChangeWaiter(path) as waiter:
                thread = threading.Thread(
                    target=lambda: result.append(waiter.wait(2)),
                )
                thread.start()
                time.sleep(0.05)
                replacement = path.with_suffix(".tmp")
                replacement.write_text("after", encoding="utf-8")
                replacement.replace(path)
                thread.join(2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [True])

    def test_append_wakes_waiter(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inbox.jsonl"
            path.write_text("", encoding="utf-8")
            result = []
            with path_watch.PathChangeWaiter(path) as waiter:
                started = time.monotonic()
                thread = threading.Thread(
                    target=lambda: result.append(waiter.wait(2)),
                )
                thread.start()
                time.sleep(0.05)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("{}\n")
                    handle.flush()
                thread.join(2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [True])
            self.assertLess(time.monotonic() - started, 0.75)

    def test_polling_fallback_still_detects_change(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text("before", encoding="utf-8")
            with mock.patch.object(
                path_watch,
                "_create_native_backend",
                return_value=None,
            ), path_watch.PathChangeWaiter(
                path,
                fallback_poll_seconds=0.01,
            ) as waiter:
                thread = threading.Thread(
                    target=lambda: (
                        time.sleep(0.05),
                        path.write_text("after", encoding="utf-8"),
                    ),
                )
                thread.start()
                self.assertTrue(waiter.wait(1))
                thread.join(1)

    def test_stop_event_interrupts_native_wait_promptly(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text("before", encoding="utf-8")
            stop = threading.Event()
            with path_watch.PathChangeWaiter(path) as waiter:
                thread = threading.Thread(
                    target=lambda: (time.sleep(0.05), stop.set()),
                )
                started = time.monotonic()
                thread.start()
                self.assertFalse(waiter.wait(5, stop))
                elapsed = time.monotonic() - started
                thread.join(1)

            self.assertLess(elapsed, 0.75)

    def test_path_set_wakes_for_each_target(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first.json"
            second = Path(temp) / "second.json"
            first.write_text("before", encoding="utf-8")
            second.write_text("before", encoding="utf-8")
            with path_watch.PathSetChangeWaiter(
                [first, second],
            ) as waiter:
                replacement = second.with_suffix(".tmp")
                thread = threading.Thread(
                    target=lambda: (
                        time.sleep(0.05),
                        replacement.write_text("after", encoding="utf-8"),
                        replacement.replace(second),
                    ),
                )
                thread.start()
                self.assertTrue(waiter.wait(2))
                thread.join(1)

    def test_path_set_ignores_unrelated_file(self):
        with tempfile.TemporaryDirectory() as temp:
            watched = Path(temp) / "watched.json"
            unrelated = Path(temp) / "unrelated.json"
            watched.write_text("before", encoding="utf-8")
            with path_watch.PathSetChangeWaiter([watched]) as waiter:
                unrelated.write_text("noise", encoding="utf-8")
                self.assertFalse(waiter.wait(0.1))
