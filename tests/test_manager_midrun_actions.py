"""A manager acts while its turn is running, not after it.

Commands execute the moment a manager runs them, so the Stemcell never sees the
actions — it only sees that the turn ended. What tells it whether the manager
actually did anything is the run record `iwantto` writes as it goes. These tests
hold the three outcomes that follow from that: acted, deliberately did nothing,
and did nothing at all.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import main
from interface import outbound as i_outbound
from interface import work_updates as i_work_updates
import manager.tracing as m_manager_tracing
import manager.turn as m_manager_turn
from diagnostics.iwantto import actor as actor_module
from diagnostics.iwantto import journal as journal_module


def _context():
    return "room_id: room-a\nevent_id: event-a\nmessage:\nhello"


class MidRunCompletionTest(unittest.TestCase):
    """Drive one manager turn and observe how the loop decides it is finished."""

    def _run_turn(self, run_record, *, output=""):
        trace = mock.Mock()
        trace.trigger = "message"
        trace.run_id = "run-message"
        trace.meta = {}
        lifecycle = mock.Mock()
        lifecycle.is_open = True
        calls = []

        def fake_call(
            carbon_id, text, _trace, _iteration, _on_tools, _on_progress, records=None
        ):
            calls.append(text)
            if records is not None:
                records[carbon_id] = dict(run_record)
            return (output, None, [])

        with (
            mock.patch.object(
                m_manager_turn, "handle_commands", side_effect=lambda value: dict(value)
            ),
            mock.patch.object(
                m_manager_turn.Diagnostics, "consume_pending_contexts", return_value=[]
            ),
            mock.patch.object(m_manager_turn.Diagnostics, "get_active_run", return_value=None),
            mock.patch.object(m_manager_turn.Diagnostics, "start_run", return_value=trace),
            mock.patch.object(m_manager_turn.Diagnostics, "register_active"),
            mock.patch.object(m_manager_turn.Diagnostics, "unregister_active"),
            mock.patch.object(
                m_manager_turn, "_instrumented_manager_call", side_effect=fake_call
            ),
            mock.patch.object(
                m_manager_turn, "queue_long_task_root_if_blocked", return_value=False
            ),
            mock.patch.object(m_manager_turn, "begin_long_task_run", return_value=lifecycle),
            mock.patch.object(m_manager_turn, "acknowledge_queued_long_task_root"),
            mock.patch.object(
                m_manager_turn, "begin_manager_activity", return_value="group-a"
            ),
            mock.patch.object(m_manager_tracing, "settle_manager_activity"),
            mock.patch.object(i_work_updates, "touch_manager_call_activity"),
            mock.patch.object(i_outbound, "send_progress"),
            mock.patch.object(i_outbound, "reply_contact") as reply_user,
            mock.patch.object(
                main, "_contact_has_active_workers", return_value=False
            ),
            mock.patch.object(m_manager_turn, "execute_all_tools") as execute_all_tools,
        ):
            execute_all_tools.return_value = ({}, {})
            m_manager_turn.run_all_managers({"carbon-a": _context()})

        return calls, lifecycle, reply_user, execute_all_tools

    def test_a_turn_that_acted_mid_run_is_finished_without_tool_json(self):
        calls, lifecycle, reply_user, execute_all_tools = self._run_turn(
            {"acted": True, "count": 3, "commands": ["send", "work", "remind"]},
            output="I sent her the summary and set a checkback.",
        )

        # One round only: no re-prompt demanding tool JSON.
        self.assertEqual(len(calls), 1)
        execute_all_tools.assert_not_called()
        reply_user.assert_not_called()
        # And the turn settles rather than hanging open.
        lifecycle.finish.assert_called_once()

    def test_a_deliberate_do_nothing_finishes_the_turn(self):
        calls, lifecycle, _reply_user, execute_all_tools = self._run_turn(
            {"did_nothing": True, "count": 1, "commands": ["do-nothing"]},
            output="Nothing needed doing.",
        )

        self.assertEqual(len(calls), 1)
        execute_all_tools.assert_not_called()
        lifecycle.finish.assert_called_once()

    def test_a_turn_that_ran_nothing_is_told_to_run_something(self):
        calls, _lifecycle, _reply_user, _execute = self._run_turn(
            {}, output="Here are my thoughts."
        )

        self.assertGreater(len(calls), 1)
        follow_up = calls[1]
        self.assertIn("without running a single iwantto command", follow_up)
        self.assertIn("iwantto do-nothing", follow_up)

    def test_tool_json_still_executes_for_provider_error_replies(self):
        """Provider failures are injected as tool JSON and must still be honoured."""
        payload = json.dumps(
            {"tools": [{"tool": "reply", "message": "Manager error: boom"}]}
        )
        _calls, _lifecycle, _reply_user, execute_all_tools = self._run_turn(
            {"acted": True}, output=payload
        )

        execute_all_tools.assert_called_once()
        submitted = execute_all_tools.call_args.args[0]
        self.assertEqual(submitted[0][1]["tool"], "reply")


class RunIdentityTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        for module, attribute, name in (
            (actor_module, "ACTORS_FILE", "actors.json"),
            (journal_module, "RUNS_FILE", "runs.json"),
        ):
            patcher = mock.patch.object(
                module, attribute, os.path.join(self._temp.name, name)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_manager_turn_runs_under_a_token_that_dies_with_it(self):
        seen = {}

        def fake_manager_code(_text, _carbon_id, **kwargs):
            env = kwargs["env"]
            seen["token"] = env[actor_module.TOKEN_ENV]
            seen["kind"] = env[actor_module.KIND_ENV]
            seen["contact"] = env[actor_module.CONTACT_ENV]
            # A command run mid-turn resolves to this manager.
            seen["resolved"] = actor_module.resolve_actor(env)
            journal_module.note_invocation(seen["token"], "send")
            return ("done", None, [])

        records = {}
        with mock.patch.object(m_manager_tracing, "manager_code", side_effect=fake_manager_code):
            m_manager_tracing._instrumented_manager_call(
                "carbon-a", "hi", None, 0, None, None, records
            )

        self.assertEqual(seen["kind"], actor_module.MANAGER)
        self.assertEqual(seen["contact"], "carbon-a")
        self.assertEqual(seen["resolved"].contact_id, "carbon-a")
        # What the turn did is captured before the token is retired.
        self.assertTrue(records["carbon-a"].get("acted"))
        self.assertEqual(records["carbon-a"].get("count"), 1)
        # And the token no longer resolves once the turn is over.
        self.assertIsNone(actor_module.lookup_token(seen["token"]))
        self.assertEqual(journal_module.run_summary(seen["token"]), {})

    def test_the_token_is_revoked_even_when_the_turn_raises(self):
        seen = {}

        def exploding(_text, _carbon_id, **kwargs):
            seen["token"] = kwargs["env"][actor_module.TOKEN_ENV]
            raise RuntimeError("provider died")

        with mock.patch.object(m_manager_tracing, "manager_code", side_effect=exploding):
            with self.assertRaises(RuntimeError):
                m_manager_tracing._instrumented_manager_call("carbon-a", "hi", None, 0, None, None)

        self.assertIsNone(actor_module.lookup_token(seen["token"]))


class LauncherTest(unittest.TestCase):
    def test_the_installed_launcher_is_executable_and_runs_the_cli(self):
        import subprocess
        import sys

        from diagnostics.iwantto import launcher

        with tempfile.TemporaryDirectory() as temp:
            bin_dir = os.path.join(temp, ".local", "bin")
            with (
                mock.patch.object(launcher, "LOCAL_BIN", bin_dir),
                mock.patch.object(
                    launcher, "LAUNCHER_PATH", os.path.join(bin_dir, "iwantto")
                ),
            ):
                path = launcher.install(python=sys.executable)

            self.assertTrue(os.access(path, os.X_OK))
            # With no identity in the environment it must refuse, not crash.
            result = subprocess.run(
                [path, "do-nothing", "--reason", "x"],
                capture_output=True,
                text=True,
                env={"PATH": os.environ.get("PATH", "")},
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("could not tell who is running it", result.stderr)

    def test_the_launcher_directory_is_already_on_the_runtime_path(self):
        """Installing is enough; nothing has to edit PATH afterwards."""
        from diagnostics.iwantto import launcher

        self.assertIn(launcher.LOCAL_BIN, os.environ["PATH"].split(os.pathsep))


if __name__ == "__main__":
    unittest.main()
