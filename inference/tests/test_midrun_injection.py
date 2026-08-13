"""Reaching a run that is already going.

A message arriving while a manager works used to wait for the whole run to
finish. Both providers can take input during a turn — Claude through streaming
stdin, Codex through `turn/steer` — and the model absorbs it at its next tool
boundary rather than being interrupted.

What these tests hold is the part that is easy to get wrong: durability. An
injected root is completed with the run it entered and retried with it if that
run fails, so speeding delivery up never costs a message.
"""
import json
import threading
import unittest
from unittest import mock

import silicon.dispatcher as m_manager_dispatcher
from inference.claude.injector import ClaudeInjector
from inference.codex.injector import CodexInjector
from iwantto import injection


class RegistryTest(unittest.TestCase):
    def tearDown(self):
        for kind, actor_id in injection.live_actors():
            injection._LIVE.pop((kind, actor_id), None)

    def test_an_offer_with_no_live_run_is_refused(self):
        self.assertFalse(injection.offer(injection.MANAGER, "carbon-a", "hi"))
        self.assertFalse(injection.is_live(injection.MANAGER, "carbon-a"))

    def test_a_live_run_receives_what_is_offered(self):
        received = []
        with injection.accepting(
            injection.MANAGER, "carbon-a", lambda text: received.append(text) or True
        ):
            self.assertTrue(injection.is_live(injection.MANAGER, "carbon-a"))
            self.assertTrue(injection.offer(injection.MANAGER, "carbon-a", "new msg"))

        self.assertEqual(received, ["new msg"])
        # And it stops being reachable once the run ends.
        self.assertFalse(injection.offer(injection.MANAGER, "carbon-a", "late"))

    def test_a_run_that_declines_is_reported_as_declined(self):
        with injection.accepting(injection.MANAGER, "carbon-a", lambda _text: False):
            self.assertFalse(injection.offer(injection.MANAGER, "carbon-a", "x"))

    def test_a_raising_injector_never_escapes_to_the_caller(self):
        def explode(_text):
            raise RuntimeError("pipe died")

        with injection.accepting(injection.MANAGER, "carbon-a", explode):
            self.assertFalse(injection.offer(injection.MANAGER, "carbon-a", "x"))

    def test_runs_for_different_contacts_do_not_cross(self):
        a, b = [], []
        with (
            injection.accepting(injection.MANAGER, "carbon-a", lambda t: a.append(t) or True),
            injection.accepting(injection.MANAGER, "carbon-b", lambda t: b.append(t) or True),
        ):
            injection.offer(injection.MANAGER, "carbon-a", "for a")
            injection.offer(injection.MANAGER, "carbon-b", "for b")

        self.assertEqual((a, b), (["for a"], ["for b"]))


class ClaudeInjectorTest(unittest.TestCase):
    class _Stdin:
        def __init__(self):
            self.written = []
            self.closed = False

        def write(self, value):
            if self.closed:
                raise ValueError("I/O operation on closed file")
            self.written.append(value)

        def flush(self):
            pass

        def close(self):
            self.closed = True

    def _injector(self):
        proc = mock.Mock()
        proc.stdin = self._Stdin()
        return ClaudeInjector(proc, "manager:carbon-a"), proc

    def test_a_message_is_written_as_one_stream_json_user_line(self):
        injector, proc = self._injector()

        self.assertTrue(injector.submit("a new message"))

        line = proc.stdin.written[0]
        self.assertTrue(line.endswith("\n"))
        payload = json.loads(line)
        self.assertEqual(payload["type"], "user")
        self.assertEqual(
            payload["message"]["content"][0]["text"], "a new message"
        )

    def test_closing_stops_acceptance_so_nothing_is_written_into_a_dead_turn(self):
        injector, proc = self._injector()
        injector.close()

        self.assertFalse(injector.submit("too late"))
        self.assertEqual(proc.stdin.written, [])
        self.assertTrue(proc.stdin.closed)

    def test_closing_twice_is_harmless(self):
        injector, _proc = self._injector()
        injector.close()
        injector.close()

    def test_a_broken_pipe_is_reported_rather_than_raised(self):
        injector, proc = self._injector()
        proc.stdin.close()

        self.assertFalse(injector.submit("x"))

    def test_concurrent_submits_do_not_interleave(self):
        injector, proc = self._injector()
        threads = [
            threading.Thread(target=injector.submit, args=(f"msg-{index}",))
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(proc.stdin.written), 20)
        for line in proc.stdin.written:
            json.loads(line)  # each write is one complete, valid line


class CodexInjectorTest(unittest.TestCase):
    def test_steering_uses_send_so_it_cannot_steal_the_turn_loops_events(self):
        """`request` drains the shared queue; an injector must never call it."""
        client = mock.Mock()
        client.send.return_value = 7
        injector = CodexInjector(
            client, "thread-1", "turn-1", "manager:carbon-a"
        )

        self.assertTrue(injector.submit("a new message"))

        client.request.assert_not_called()
        method, params = client.send.call_args.args
        self.assertEqual(method, "turn/steer")
        self.assertEqual(params["threadId"], "thread-1")
        self.assertEqual(params["expectedTurnId"], "turn-1")
        self.assertEqual(params["input"], [{"type": "text", "text": "a new message"}])

    def test_a_turn_without_an_id_cannot_be_steered(self):
        injector = CodexInjector(mock.Mock(), "thread-1", "", "tag")

        self.assertFalse(injector.submit("x"))

    def test_closing_stops_acceptance(self):
        client = mock.Mock()
        injector = CodexInjector(client, "thread-1", "turn-1", "tag")
        injector.close()

        self.assertFalse(injector.submit("x"))
        client.send.assert_not_called()


class DispatcherDurabilityTest(unittest.TestCase):
    """An injected root must share the fate of the run it was injected into."""

    def _dispatcher(self):
        dispatcher = m_manager_dispatcher.ManagerDispatcher(runner=lambda _contexts: None)
        dispatcher._running.add("carbon-a")
        return dispatcher

    @staticmethod
    def _admission(context="new message"):
        admission = mock.Mock(spec=m_manager_dispatcher.RootAdmission)
        admission.contact_id = "carbon-a"
        admission.context = context
        return admission

    def test_a_busy_contact_takes_the_message_now_instead_of_queueing_it(self):
        dispatcher = self._dispatcher()
        received = []

        with injection.accepting(
            injection.MANAGER, "carbon-a", lambda t: received.append(t) or True
        ):
            dispatcher._schedule_admissions([self._admission()])

        # Delivered into the live run, framed as a new trigger...
        self.assertEqual(len(received), 1)
        self.assertIn("NEW MESSAGE", received[0])
        self.assertIn("new message", received[0])
        # ...and not left waiting for a second run.
        self.assertEqual(dispatcher._pending.get("carbon-a", []), [])
        self.assertEqual(len(dispatcher._injected["carbon-a"]), 1)

    def test_a_refused_offer_falls_back_to_the_normal_queue(self):
        dispatcher = self._dispatcher()

        with injection.accepting(injection.MANAGER, "carbon-a", lambda _t: False):
            dispatcher._schedule_admissions([self._admission()])

        self.assertEqual(len(dispatcher._pending["carbon-a"]), 1)
        self.assertEqual(dispatcher._injected.get("carbon-a", []), [])

    def test_an_idle_contact_is_never_injected_into(self):
        dispatcher = m_manager_dispatcher.ManagerDispatcher(runner=lambda _contexts: None)
        received = []

        with injection.accepting(
            injection.MANAGER, "carbon-a", lambda t: received.append(t) or True
        ):
            with mock.patch.object(m_manager_dispatcher.threading, "Thread"):
                dispatcher._schedule_admissions([self._admission()])

        self.assertEqual(received, [])
        self.assertEqual(len(dispatcher._pending["carbon-a"]), 1)

    def test_injected_roots_complete_with_the_run_they_entered(self):
        dispatcher = self._dispatcher()
        injected = self._admission()
        dispatcher._injected["carbon-a"] = [injected]

        taken = dispatcher._take_injected("carbon-a")

        self.assertEqual(taken, [injected])
        self.assertEqual(dispatcher._take_injected("carbon-a"), [])


if __name__ == "__main__":
    unittest.main()
