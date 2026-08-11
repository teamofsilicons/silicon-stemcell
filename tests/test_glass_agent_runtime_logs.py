import tempfile
import unittest
from pathlib import Path

from interface.agent import live as glass_agent


class RuntimeLogTailerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tempdir.name) / ".silicon.log"
        self.frames = []
        self.tailer = glass_agent.RuntimeLogTailer(self.log_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def poll(self):
        return self.tailer.poll(self.frames.append)

    def test_initial_tail_matches_debug_then_streams_new_lines(self):
        self.log_path.write_text(
            "".join(f"line {index}\n" for index in range(12)),
            encoding="utf-8",
        )

        self.assertEqual(self.poll(), 10)
        self.assertEqual(
            [frame["msg"] for frame in self.frames],
            [f"line {index}" for index in range(2, 12)],
        )

        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write("[Silicon] Warning: retrying\n")
            handle.write("[Silicon] Error: request failed\n")

        self.assertEqual(self.poll(), 2)
        self.assertEqual(self.frames[-2]["level"], "warn")
        self.assertEqual(self.frames[-1]["level"], "error")
        self.assertTrue(all(frame["source"] == "silicon" for frame in self.frames))
        self.assertTrue(all(frame["type"] == "log" for frame in self.frames))

    def test_partial_line_waits_for_the_process_to_finish_it(self):
        self.log_path.write_text("partial", encoding="utf-8")
        self.assertEqual(self.poll(), 0)

        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(" message\n")

        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.frames[-1]["msg"], "partial message")

    def test_copy_truncate_and_replacement_are_detected(self):
        self.log_path.write_text("old one\nold two\n", encoding="utf-8")
        self.assertEqual(self.poll(), 2)

        self.log_path.write_text("new file after truncate\n", encoding="utf-8")
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.frames[-1]["msg"], "new file after truncate")

        replacement = self.log_path.with_suffix(".replacement")
        replacement.write_text("new inode\n", encoding="utf-8")
        replacement.replace(self.log_path)
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.frames[-1]["msg"], "new inode")

    def test_failed_send_is_replayed_after_reconnect(self):
        self.log_path.write_text("deliver me\n", encoding="utf-8")

        def fail(_frame):
            raise ConnectionError("socket closed")

        with self.assertRaises(ConnectionError):
            self.tailer.poll(fail)

        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.frames[-1]["msg"], "deliver me")


if __name__ == "__main__":
    unittest.main()


class OutboundFrameBoundTests(unittest.TestCase):
    """No frame may exceed Glass's inbound cap and cost the control channel.

    Glass runs uvicorn with --ws-max-size 131072; a larger frame is refused at
    the transport with close 1009, which the server application never sees. That
    is how one silicon lost its socket every second for hours.
    """

    def test_small_frames_pass_through_untouched(self):
        frame = {"type": "ping", "ts": 1}
        self.assertIs(glass_agent.bound_frame(frame), frame)

    def test_oversized_field_is_truncated_not_dropped(self):
        frame = {
            "type": "command_result",
            "id": "cmd-1",
            "command": "backup",
            "status": "done",
            "message": "x" * 400_000,
        }
        bounded = glass_agent.bound_frame(frame)

        self.assertIsNotNone(bounded)
        self.assertLessEqual(
            glass_agent._frame_size(bounded), glass_agent.MAX_OUTBOUND_FRAME_BYTES
        )
        # Routing/identity survives so the receiver can still act on it.
        self.assertEqual(bounded["id"], "cmd-1")
        self.assertEqual(bounded["command"], "backup")
        self.assertEqual(bounded["status"], "done")
        self.assertIn("truncated", bounded["message"])

    def test_oversized_list_is_replaced_with_a_count_marker(self):
        frame = {"type": "status", "items": [{"blob": "y" * 500} for _ in range(1000)]}
        bounded = glass_agent.bound_frame(frame)

        self.assertIsNotNone(bounded)
        self.assertLessEqual(
            glass_agent._frame_size(bounded), glass_agent.MAX_OUTBOUND_FRAME_BYTES
        )
        self.assertEqual(bounded["items"], [{"truncated_items": 1000}])

    def test_send_json_reports_failure_instead_of_raising(self):
        sent = []

        class FakeWS:
            def send(self, data):
                sent.append(data)

        ws = FakeWS()
        self.assertTrue(glass_agent.send_json(ws, {"type": "ping", "ts": 1}))
        self.assertEqual(len(sent), 1)

        # A frame that is all protected keys cannot be shrunk -- it must be
        # reported, never handed to the socket.
        huge_protected = {"type": "x" * 400_000}
        self.assertFalse(glass_agent.send_json(ws, huge_protected))
        self.assertEqual(len(sent), 1, "oversized frame must not reach the socket")

    def test_every_bounded_frame_fits_the_server_limit(self):
        for frame in (
            {"type": "log", "msg": "z" * 300_000, "level": "info", "source": "silicon"},
            {"type": "diag.rollup", "run_id": "r1", "events": [{"e": "q" * 300}] * 2000},
            {"type": "terminal", "session_id": "s1", "data": "t" * 250_000},
        ):
            bounded = glass_agent.bound_frame(frame)
            self.assertIsNotNone(bounded, frame["type"])
            self.assertLessEqual(
                glass_agent._frame_size(bounded),
                glass_agent.MAX_OUTBOUND_FRAME_BYTES,
                frame["type"],
            )
