"""The diagnosis store — everything that happens inside this Silicon.

TODO.md names four things it must hold, so a session can be reconstructed when
something goes wrong: every manager invocation, every command run, every message
sent, and every file written. These tests hold each of the four against the
place it is actually captured.
"""
import os
import tempfile
import unittest
from unittest import mock

from inference import telemetry
from inference.claude import stream as claude_stream
from diagnostics.iwantto import actor as actor_module
from diagnostics.iwantto import journal


class _IsolatedJournal(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        for module, attribute, name in (
            (journal, "DIAGNOSIS_DIR", "diagnosis"),
            (journal, "RUNS_FILE", "runs.json"),
            (actor_module, "ACTORS_FILE", "actors.json"),
        ):
            patcher = mock.patch.object(
                module, attribute, os.path.join(self._temp.name, name)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def events(self, kind=None):
        entries = journal.read_recent(limit=0)
        if kind is None:
            return entries
        return [entry for entry in entries if entry.get("event") == kind]


class ManagerInvocationTest(_IsolatedJournal):
    def test_every_manager_turn_is_recorded_with_what_it_did(self):
        import main

        def fake_manager_code(_text, _carbon_id, **kwargs):
            token = kwargs["env"][actor_module.TOKEN_ENV]
            journal.note_invocation(token, "send")
            journal.note_invocation(token, "work")
            return ("done", None, [])

        with mock.patch.object(main, "manager_code", side_effect=fake_manager_code):
            main._instrumented_manager_call("carbon-a", "hello", None, 0, None, None)

        runs = self.events(journal.RUN)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["kind"], "manager")
        self.assertEqual(runs[0]["contact_id"], "carbon-a")
        self.assertEqual(runs[0]["trigger"], "hello")
        self.assertEqual(runs[0]["commands"], ["send", "work"])
        self.assertTrue(runs[0]["ok"])
        self.assertIsInstance(runs[0]["seconds"], float)

    def test_a_failed_manager_turn_is_recorded_as_failed(self):
        import main

        with mock.patch.object(
            main, "manager_code", side_effect=RuntimeError("provider died")
        ):
            with self.assertRaises(RuntimeError):
                main._instrumented_manager_call("carbon-a", "hi", None, 0, None, None)

        runs = self.events(journal.RUN)
        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0]["ok"])
        self.assertEqual(runs[0]["detail"], "RuntimeError")

    def test_undirected_brain_failure_is_suppressed(self):
        import main

        provider_failure = (
            '{"tools": [{"tool": "reply", '
            '"message": "Codex not authenticated."}]}'
        )
        records = {("visible_activity", "carbon-a", 0): False}
        with (
            mock.patch.object(
                main,
                "manager_code",
                return_value=(provider_failure, None, []),
            ),
            mock.patch.object(
                main,
                "provider_failed",
                return_value=True,
            ),
        ):
            output, rate_limit, executed = main._instrumented_manager_call(
                "carbon-a",
                "internal root",
                None,
                0,
                None,
                None,
                records,
            )

        self.assertEqual(
            output,
            '{"tools": [{"tool": "do_nothing"}]}',
        )
        self.assertIsNone(rate_limit)
        self.assertEqual(executed, [])

    def test_visible_brain_failure_is_preserved(self):
        import main

        provider_failure = (
            '{"tools": [{"tool": "reply", '
            '"message": "Usage limit reached."}]}'
        )
        with mock.patch.object(
            main,
            "provider_failed",
            return_value=True,
        ):
            result = main._suppress_undirected_brain_failure(
                (provider_failure, True, []),
                "carbon-a",
                True,
            )

        self.assertEqual(result, (provider_failure, True, []))
        self.assertTrue(main._is_terminal_brain_failure(provider_failure))
        self.assertFalse(
            main._is_terminal_brain_failure(
                '{"tools": [{"tool": "reply", "message": "Try again."}]}'
            )
        )

    def test_every_advisor_turn_is_recorded(self):
        from manager import advisor

        with (
            mock.patch.object(advisor, "_rotate_session"),
            mock.patch.object(advisor, "ADVISOR_STATE_FILE",
                              os.path.join(self._temp.name, "advisors.json")),
            mock.patch("diagnostics.iwantto.actor.issue_run_env", return_value=("t", {})),
            mock.patch("diagnostics.iwantto.actor.revoke_actor"),
            mock.patch("manager.run_agent", return_value="Delegate it."),
        ):
            advisor.ask("carbon-a", "should I?")

        runs = self.events(journal.RUN)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["kind"], "advisor")
        self.assertEqual(runs[0]["contact_id"], "carbon-a")
        self.assertFalse(runs[0]["heartbeat"])

    def test_every_worker_run_is_recorded(self):
        import worker.handler as worker_handler

        worker_handler._worker_process_env("carbon-a", "researcher", "browser")

        runs = self.events(journal.RUN)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["kind"], "worker")
        self.assertEqual(runs[0]["actor_id"], "researcher")
        self.assertEqual(runs[0]["worker_type"], "browser")


class MessagesSentTest(_IsolatedJournal):
    def test_a_direct_message_is_recorded_with_its_msgid(self):
        journal.record_message(
            "out", "carbon-a", via="interface", event_id="event-1", body="hey"
        )

        messages = self.events(journal.MESSAGE)
        self.assertEqual(messages[0]["contact_id"], "carbon-a")
        self.assertEqual(messages[0]["event_id"], "event-1")
        self.assertEqual(messages[0]["via"], "interface")
        self.assertEqual(messages[0]["body"], "hey")

    def test_a_routed_manager_message_is_recorded(self):
        from interface import messages as messages_module

        with (
            mock.patch.object(messages_module, "_queue_lineage_handoff", return_value=False),
            mock.patch.object(messages_module, "_append_manager_queue_item"),
            mock.patch("interface.adapter.notify_runtime_activity"),
        ):
            messages_module.send_manager_message(
                "carbon-a", "carbon-b", "can you help?", target_type="carbon"
            )

        messages = self.events(journal.MESSAGE)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["contact_id"], "carbon-b")
        self.assertEqual(messages[0]["via"], "manager_queue")
        self.assertEqual(messages[0]["sender"], "carbon-a")
        self.assertEqual(messages[0]["body"], "can you help?")

    def test_a_worker_message_is_recorded_under_its_own_label(self):
        from interface import messages as messages_module

        with (
            mock.patch.object(messages_module, "_queue_lineage_handoff", return_value=False),
            mock.patch.object(messages_module, "_append_manager_queue_item"),
            mock.patch("interface.adapter.notify_runtime_activity"),
        ):
            messages_module.send_manager_message(
                "researcher",
                "carbon-a",
                "where do I save this?",
                sender_label="browser worker `researcher`",
            )

        messages = self.events(journal.MESSAGE)
        self.assertEqual(messages[0]["sender"], "browser worker `researcher`")


class FilesWrittenTest(_IsolatedJournal):
    """The provider's own Write/Edit tools change files, so the stream is the
    only place a file write becomes visible to Silicon at all."""

    def _write_progress(self, path="/data/prompts/MEMORY.md", tool="Write"):
        from interface.progress import WRITING_FILE, progress_event

        return progress_event(
            "claude",
            WRITING_FILE,
            status="started",
            item_id="tool-1",
            tool_name=tool,
            path=path,
        )

    def test_a_file_write_is_attributed_to_the_run_that_made_it(self):
        env = {
            actor_module.KIND_ENV: "manager",
            actor_module.ID_ENV: "carbon-a",
            actor_module.CONTACT_ENV: "carbon-a",
        }

        telemetry.record_file_write(self._write_progress(), env, "manager:carbon-a")

        writes = self.events(journal.FILE_WRITE)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["path"], "/data/prompts/MEMORY.md")
        self.assertEqual(writes[0]["tool"], "Write")
        self.assertEqual(writes[0]["kind"], "manager")
        self.assertEqual(writes[0]["contact_id"], "carbon-a")

    def test_reads_and_completions_are_not_recorded_as_writes(self):
        from interface.progress import READING_FILE, WRITING_FILE, progress_event

        telemetry.record_file_write(
            progress_event("claude", READING_FILE, status="started",
                           tool_name="Read", path="/data/prompts/MEMORY.md"),
            {}, "tag",
        )
        # The completion event for a write must not double-count it.
        telemetry.record_file_write(
            progress_event("claude", WRITING_FILE, status="completed",
                           tool_name="Write", path="/data/prompts/MEMORY.md"),
            {}, "tag",
        )

        self.assertEqual(self.events(journal.FILE_WRITE), [])

    def test_the_streaming_loop_captures_writes_as_they_happen(self):
        """End to end through the streaming loop, not just the helper."""
        import json as json_module

        events = [
            json_module.dumps({
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Edit",
                    "input": {"file_path": "/data/prompts/LORE.md"},
                }]},
            }) + "\n",
            "",
        ]

        class Stdin:
            def write(self, _value): return None
            def close(self): return None

        class Stdout:
            def readline(self): return events.pop(0) if events else ""

        class Stderr:
            def read(self): return ""

        class Process:
            stdin, stdout, stderr = Stdin(), Stdout(), Stderr()
            def wait(self): return 0

        env = {
            actor_module.KIND_ENV: "advisor",
            actor_module.ID_ENV: "carbon-a",
            actor_module.CONTACT_ENV: "carbon-a",
        }
        with (
            mock.patch.object(claude_stream.subprocess, "Popen", return_value=Process()),
            mock.patch("builtins.print"),
        ):
            claude_stream.run_streaming(
                ["provider"], "", "advisor:carbon-a", cwd=None, env=env
            )

        writes = self.events(journal.FILE_WRITE)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["path"], "/data/prompts/LORE.md")
        self.assertEqual(writes[0]["tool"], "Edit")
        self.assertEqual(writes[0]["kind"], "advisor")


class StoreShapeTest(_IsolatedJournal):
    def test_all_four_kinds_land_in_one_readable_trail(self):
        actor = actor_module.Actor(
            kind="manager", actor_id="carbon-a", contact_id="carbon-a", token="t"
        )
        journal.record(actor, "send", args=["send", "x"], result="Sent")
        journal.record_run("manager", "carbon-a", "carbon-a", trigger="hello")
        journal.record_message("out", "carbon-a", event_id="e1", body="hi")
        journal.record_file_write("/data/prompts/MEMORY.md", kind="manager")

        kinds = [entry["event"] for entry in self.events()]
        self.assertEqual(
            sorted(kinds),
            sorted([journal.COMMAND, journal.RUN, journal.MESSAGE, journal.FILE_WRITE]),
        )

    def test_a_broken_store_never_breaks_the_thing_being_recorded(self):
        with mock.patch.object(
            journal, "_today_path", side_effect=OSError("disk full")
        ):
            journal.record_run("manager", "carbon-a", "carbon-a")
            journal.record_message("out", "carbon-a")
            journal.record_file_write("/x")
        # No exception escaped, and nothing was written.
        self.assertEqual(self.events(), [])

    def test_long_bodies_are_bounded(self):
        journal.record_message("out", "carbon-a", body="x" * (journal.MAX_FIELD_CHARS * 2))

        body = self.events(journal.MESSAGE)[0]["body"]
        self.assertLess(len(body), journal.MAX_FIELD_CHARS * 2)
        self.assertIn("…(+", body)


if __name__ == "__main__":
    unittest.main()


class AdvertisingPublishTest(unittest.TestCase):
    """`prompts/ADVERTISING.md` is a plain file the Silicon writes; something
    still has to carry it to the team."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = self._temp.name
        os.makedirs(os.path.join(self.root, "prompts"))
        patcher = mock.patch("helpers.paths.DATA_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, text):
        with open(os.path.join(self.root, "prompts", "ADVERTISING.md"), "w") as handle:
            handle.write(text)

    def test_a_changed_file_is_published_once(self):
        import config

        self._write("I do market research.")
        with mock.patch(
            "interface.team_context.update_own_advertising_memory",
            return_value={"ok": True, "status": "uploaded", "revision": 1},
        ) as publish:
            first = config._publish_own_advertising()
            second = config._publish_own_advertising()

        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[0], "I do market research.")
        self.assertTrue(first["ok"])
        self.assertIsNone(second, "an unchanged file must not burn a revision")

    def test_editing_the_file_publishes_again(self):
        import config

        self._write("v1")
        with mock.patch(
            "interface.team_context.update_own_advertising_memory",
            return_value={"ok": True, "status": "uploaded"},
        ) as publish:
            config._publish_own_advertising()
            self._write("v2")
            config._publish_own_advertising()

        self.assertEqual(
            [call.args[0] for call in publish.call_args_list], ["v1", "v2"]
        )

    def test_a_failed_publish_is_retried_next_tick(self):
        import config

        self._write("v1")
        with mock.patch(
            "interface.team_context.update_own_advertising_memory",
            return_value={"ok": False, "status": "pending"},
        ) as publish:
            config._publish_own_advertising()
            config._publish_own_advertising()

        self.assertEqual(publish.call_count, 2)

    def test_a_missing_file_is_not_an_error(self):
        import config

        self.assertIsNone(config._publish_own_advertising())
