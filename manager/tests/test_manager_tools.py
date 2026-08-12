import argparse
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import inference
import inference.sessions
import manager.activity as m_manager_activity
import manager.tools.memory as m_manager_tools_memory
from interface import outbound as i_outbound
from interface import work as i_work_updates
import manager.tools.browser as m_manager_tools_browser
import manager.tools.messaging as m_manager_tools_messaging
import manager.tools.work as m_manager_tools_work
import manager.tools.worker as m_manager_tools_worker
import manager.settings as m_manager_settings
import manager.tools.helpers as m_manager_tools_helpers
import manager.tools.registry as m_manager_tools_registry
import manager.tracing as m_manager_tracing
import manager.turn as m_manager_turn
import manager
from interface.long_tasks import registry as lt_registry
from prompts import loader as DNA
from diagnostics import activity as activity_log
from inference.claude import stream as claude_stream
from inference.codex import provider as codex_provider
from inference.codex.redact import redact_agent_message


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ManagerToolsDocTest(unittest.TestCase):
    """A manager's tools are `iwantto` commands now, not end-of-turn tool JSON."""

    def test_every_command_the_prompts_promise_exists_in_the_cli(self):
        from iwantto.cli import build_parser

        text = "\n".join(
            Path(DNA._prompt_path(name)).read_text(encoding="utf-8")
            for name in ("MANAGER_TOOLS.md", "IWANTTO_CLI_REFERENCE.md")
        )
        # Only backticked command spans; prose like "the iwantto cli" is not a
        # promise that a subcommand named `cli` exists.
        documented = set(re.findall(r"`iwantto ([a-z][a-z-]+)", text))
        self.assertTrue(documented, "the prompts document no iwantto commands")

        subparser_action = next(
            action
            for action in build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        implemented = set(subparser_action.choices)

        self.assertEqual(
            sorted(documented - implemented),
            [],
            "the prompts promise commands the CLI does not implement",
        )

    def test_every_flag_the_reference_promises_is_accepted(self):
        """IWANTTO_CLI_REFERENCE.md is the final authority.

        TODO.md: "iwantto cli reference is the most imp file and the only final
        file. if you notice any descripency – then update it in accordance to
        the iwantto cli reference." This is that check, mechanically.
        """
        from iwantto.cli import build_parser

        reference = Path(
            DNA._prompt_path("IWANTTO_CLI_REFERENCE.md")
        ).read_text(encoding="utf-8")
        subparsers = next(
            action
            for action in build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        )

        gaps = []
        for match in re.finditer(r"`iwantto ([a-z][a-z-]*)([^`]*)`", reference):
            command, rest = match.group(1), match.group(2)
            if command not in subparsers.choices:
                gaps.append((command, "<command missing>"))
                continue
            accepted = {
                option
                for action in subparsers.choices[command]._actions
                for option in action.option_strings
            }
            gaps.extend(
                (command, flag)
                for flag in re.findall(r"(--[a-z][a-z-]*)", rest)
                if flag not in accepted
            )

        self.assertEqual(sorted(set(gaps)), [])

    def test_manager_tools_prompt_carries_no_tool_json(self):
        text = Path(DNA._prompt_path("MANAGER_TOOLS.md")).read_text(encoding="utf-8")
        self.assertNotIn('"tools"', text)
        self.assertIn("iwantto", text)


class ManagerActivityVisibilityTest(unittest.TestCase):
    @staticmethod
    def _run_root(trigger):
        trace = mock.Mock()
        trace.trigger = trigger
        trace.run_id = f"run-{trigger}"
        trace.meta = {}
        context = (
            "room_id: room-a\nevent_id: event-a\nmessage:\nhello"
            if trigger == "message"
            else "internal manager work"
        )
        pending_contexts = (
            [
                {
                    "handoff_id": "handoff-a",
                    "source_run_id": "source-a",
                    "target_type": "silicon",
                    "target_id": "carbon-a",
                }
            ]
            if trigger == "handoff"
            else []
        )
        terminal = (
            json.dumps({"tools": [{"tool": "do_nothing"}]}),
            None,
            [],
        )
        with (
            mock.patch.object(m_manager_turn, "handle_commands", side_effect=lambda value: value),
            mock.patch.object(
                m_manager_turn.Diagnostics,
                "consume_pending_contexts",
                return_value=pending_contexts,
            ),
            mock.patch.object(m_manager_turn.Diagnostics, "get_active_run", return_value=None),
            mock.patch.object(m_manager_turn.Diagnostics, "start_run", return_value=trace),
            mock.patch.object(m_manager_turn.Diagnostics, "register_active"),
            mock.patch.object(m_manager_turn.Diagnostics, "unregister_active"),
            mock.patch.object(
                m_manager_turn,
                "_instrumented_manager_call",
                return_value=terminal,
            ),
            mock.patch.object(
                m_manager_turn,
                "begin_manager_activity",
                return_value=f"group-{trigger}",
            ) as begin_activity,
            mock.patch.object(m_manager_tracing, "settle_manager_activity") as settle_activity,
            mock.patch.object(i_work_updates, "touch_manager_call_activity"),
            mock.patch.object(m_manager_turn, "set_active_task_timer"),
            mock.patch.object(i_outbound, "send_progress") as send_progress,
        ):
            m_manager_turn.run_all_managers({"carbon-a": context})
        return begin_activity, settle_activity, send_progress

    def test_direct_carbon_root_exposes_one_settled_activity_group(self):
        begin_activity, settle_activity, send_progress = self._run_root("message")

        begin_activity.assert_called_once_with("carbon-a", "run-message")
        settle_activity.assert_called_once_with("carbon-a", "group-message")
        self.assertEqual(
            [call.args[2:4] for call in send_progress.call_args_list],
            [
                ("thinking", "calling manager"),
                ("done", "manager finished"),
            ],
        )

    def test_internal_roots_do_not_expose_manager_activity(self):
        for trigger in ("handoff", "manager_loop"):
            with self.subTest(trigger=trigger):
                begin_activity, settle_activity, send_progress = self._run_root(
                    trigger
                )
                begin_activity.assert_not_called()
                settle_activity.assert_not_called()
                send_progress.assert_not_called()

    def test_internal_run_keeps_provider_and_tool_progress_diagnostic_only(self):
        trace = mock.Mock()
        trace.meta = {"_manager_running": True}
        with (
            mock.patch.object(
                m_manager_tools_helpers,
                "current_manager_activity_group",
                return_value="",
            ),
            mock.patch.object(m_manager_turn.Diagnostics, "get_active_run", return_value=trace),
            mock.patch.object(i_outbound, "send_progress") as send_progress,
            mock.patch.object(
                m_manager_tools_work,
                "execute_work_update",
                return_value="Done. internal update",
            ),
            mock.patch.object(
                i_work_updates,
                "touch_manager_call_activity",
            ) as touch_manager_call_activity,
        ):
            handler = m_manager_tracing._make_provider_progress_handler("carbon-a", "")
            handler(
                {
                    "provider": "codex",
                    "kind": "command",
                    "status": "started",
                    "item_id": "command-a",
                },
                "running internal command",
            )
            result = m_manager_tools_registry._execute_single_tool(
                {"tool": "work_update", "action": "task/update"},
                "carbon-a",
            )
            failure = m_manager_tools_helpers._message_failure_status(
                "carbon-a",
                "silicon",
                "peer-a",
                "offline",
            )

        send_progress.assert_not_called()
        touch_manager_call_activity.assert_called_once_with("carbon-a")
        trace.event.assert_called_once()
        self.assertIn("Done. internal update", result)
        self.assertIn("offline", failure)


class ManagerToolExecutionTest(unittest.TestCase):
    def setUp(self):
        # A lifecycle left registered by another test changes which branch a
        # tool takes. Start every case with an empty registry.
        lt_registry.reset_long_task_registry_for_tests()
        self.addCleanup(lt_registry.reset_long_task_registry_for_tests)

    def test_codex_inactivity_timeout_resets_thread_without_sending_reply(self):
        class FakeProcess:
            @staticmethod
            def poll():
                return None

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                self.proc = FakeProcess()
                self.messages = mock.Mock()
                self.closed = False

            def request(self, method, _params, timeout=None):
                del timeout
                if method == "thread/resume":
                    return {
                        "result": {
                            "thread": {"id": "thread-a"},
                            "model": "test-model",
                            "modelProvider": "openai",
                        }
                    }
                if method == "turn/start":
                    return {"result": {"turn": {"id": "turn-a"}}}
                return {"result": {}}

            @staticmethod
            def _handle_server_request(_message):
                return False

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temp:
            thread_file = Path(temp) / "carbon-a_codex.txt"
            thread_file.write_text("thread-a", encoding="utf-8")
            fake_client = FakeClient()
            with (
                mock.patch.object(inference.sessions, "SESSIONS_DIR", temp),
                mock.patch.object(codex_provider, "TracedAppServer",
                                  return_value=fake_client),
                mock.patch.object(codex_provider, "INACTIVITY_TIMEOUT", 0.0),
                mock.patch("interface.reply_contact") as reply_contact,
            ):
                provider = codex_provider.CodexProvider()
                with self.assertRaises(inference.ProviderTimeoutError):
                    provider.run_turn(inference.TurnRequest(
                        text="hello",
                        contact_id="carbon-a",
                        system_prompt="system prompt",
                    ))

            reply_contact.assert_not_called()
            self.assertFalse(thread_file.exists())
            self.assertTrue(fake_client.closed)

    def test_manager_timeout_is_retried_once_then_paused(self):
        trace = mock.Mock()
        trace.trigger = "message"
        trace.run_id = "run-message"
        trace.meta = {}
        lifecycle = mock.Mock()
        lifecycle.is_open = True
        timeout_result = (manager.TIMEOUT_MSG, None, [])

        with (
            mock.patch.object(
                m_manager_turn,
                "handle_commands",
                side_effect=lambda contexts: dict(contexts),
            ),
            mock.patch.object(
                m_manager_turn.Diagnostics,
                "consume_pending_contexts",
                return_value=[],
            ),
            mock.patch.object(
                m_manager_turn.Diagnostics,
                "get_active_run",
                return_value=None,
            ),
            mock.patch.object(
                m_manager_turn.Diagnostics,
                "start_run",
                return_value=trace,
            ),
            mock.patch.object(m_manager_turn.Diagnostics, "register_active"),
            mock.patch.object(m_manager_turn.Diagnostics, "unregister_active"),
            mock.patch.object(
                m_manager_turn,
                "_instrumented_manager_call",
                side_effect=[timeout_result, timeout_result],
            ) as manager_call,
            mock.patch.object(
                m_manager_turn,
                "queue_long_task_root_if_blocked",
                return_value=False,
            ),
            mock.patch.object(
                m_manager_turn,
                "begin_long_task_run",
                return_value=lifecycle,
            ),
            mock.patch.object(
                m_manager_turn,
                "acknowledge_queued_long_task_root",
            ),
            mock.patch.object(
                m_manager_turn,
                "begin_manager_activity",
                return_value="group-message",
            ),
            mock.patch.object(m_manager_tracing, "settle_manager_activity"),
            mock.patch.object(i_work_updates, "touch_manager_call_activity"),
            mock.patch.object(i_outbound, "send_progress"),
            mock.patch.object(i_outbound, "reply_contact") as reply_user,
            mock.patch.object(
                m_manager_activity,
                "_contact_has_active_workers",
                return_value=False,
            ),
        ):
            m_manager_turn.run_all_managers(
                {
                    "carbon-a": (
                        "room_id: room-a\n"
                        "event_id: event-a\n"
                        "message:\nhello"
                    )
                }
            )

        self.assertEqual(manager_call.call_count, 2)
        self.assertEqual(
            reply_user.call_args_list,
            [
                mock.call(
                    m_manager_settings.MANAGER_TIMEOUT_RETRY_REPLY,
                    "carbon-a",
                    work_continues=True,
                ),
                mock.call(
                    m_manager_settings.MANAGER_TIMEOUT_FINAL_REPLY,
                    "carbon-a",
                ),
            ],
        )
        lifecycle.defer.assert_called_once_with(
            "Work paused after the manager provider stopped responding twice",
            pause_reason="infrastructure",
        )
        lifecycle.finish.assert_called_once()

    def test_parser_preserves_braces_quotes_and_fences_in_advertising_content(self):
        contents = [
            'Use {"state": {"ready": true}}.',
            "Opening { and closing } braces are ordinary Markdown.",
            'Quoted value: "ready".',
            "Example:\n```json\n{\"ready\": true}\n```",
        ]
        for content in contents:
            with self.subTest(content=content):
                payload = json.dumps(
                    {
                        "tools": [
                            {
                                "tool": "advertising_memory/update",
                                "content": content,
                            }
                        ]
                    }
                )
                parsed = manager.parse_manager_output(
                    f"Here is the update:\n```json\n{payload}\n```\nDone.",
                )
                self.assertEqual(parsed["tools"][0]["content"], content)

    def test_parser_decodes_escaped_private_tool_name_without_debug_output(self):
        output = (
            '{"tools":[{"tool":"advertising_memory\\u002fupdate",'
            '"content":"do not print me"}]}'
        )

        with mock.patch("builtins.print") as print_output:
            parsed = manager.parse_manager_output(output)

        print_output.assert_not_called()
        self.assertEqual(
            parsed["tools"][0]["tool"],
            "advertising_memory/update",
        )

    def test_worker_new_requires_worker_id_and_task(self):
        missing_id = m_manager_tools_registry.execute_single_tool(
            {"tool": "worker/browser", "type": "new", "task": "research"},
            "carbon-a",
        )
        self.assertEqual(missing_id, "Tool 'worker/new': Error: worker-id is required")

        missing_task = m_manager_tools_registry.execute_single_tool(
            {"tool": "worker/browser", "type": "new", "worker-id": "researcher"},
            "carbon-a",
        )
        self.assertEqual(missing_task, "Tool 'worker/new' (researcher): Error: task is required")

    def test_trust_list_refreshes_the_canonical_glass_projection(self):
        snapshot = {
            "status": "current",
            "revision": "7:2",
            "entries": [
                {
                    "kind": "silicon",
                    "id": "peer-si",
                    "level": "high",
                    "source": "team_base",
                }
            ],
        }
        with (
            mock.patch(
                "interface.trust.inspect_trust_policy",
                return_value=snapshot,
            ) as inspect,
            mock.patch.object(i_outbound, "send_progress"),
        ):
            result = m_manager_tools_registry.execute_single_tool(
                {"tool": "trust/list"},
                "carbon-a",
            )

        inspect.assert_called_once_with(
            kind="",
            public_id="",
            root=m_manager_settings.PROJECT_ROOT,
            refresh=True,
        )
        self.assertIn('"revision": "7:2"', result)
        self.assertIn('"id": "peer-si"', result)

    def test_trust_get_requires_one_typed_identity(self):
        result = m_manager_tools_registry.execute_single_tool(
            {"tool": "trust/get"},
            "carbon-a",
        )

        self.assertEqual(
            result,
            (
                "Tool 'trust/get': Error: provide exactly one of carbon_id "
                "or silicon_id"
            ),
        )

    def test_worker_new_dispatches_documented_fields(self):
        spec = {
            "tool": "worker/browser",
            "type": "new",
            "worker-id": "public-research",
            "task": "Research the page.",
            "incognito": True,
            "checkback_in": 5,
        }

        with (
            mock.patch.object(m_manager_tools_worker, "start_worker", return_value="Done. started") as start_worker,
            mock.patch.object(m_manager_tools_worker, "add_checkback") as add_checkback,
            mock.patch.object(i_outbound, "send_progress") as send_progress,
        ):
            result = m_manager_tools_registry.execute_single_tool(spec, "carbon-a")

        start_worker.assert_called_once_with(
            "public-research",
            "Research the page.",
            "browser",
            "carbon-a",
            incognito=True,
        )
        add_checkback.assert_called_once_with("public-research", "carbon-a", 5.0)
        send_progress.assert_called()
        self.assertIn("Tool 'worker/new' (browser, public-research): Done. started", result)
        self.assertIn("checkback in 5 min", result)

    def test_worker_new_returns_automatically_bridged_invocation_ids(self):
        spec = {
            "tool": "worker/terminal",
            "type": "new",
            "worker-id": "builder",
            "task": "Build the app.",
        }
        reference = {
            "task_id": "task-fitness",
            "group_id": "group-build",
            "invocation_id": "invocation-builder-1",
        }

        with (
            mock.patch.object(m_manager_tools_worker, "start_worker", return_value="Done. started"),
            mock.patch.object(
                m_manager_tools_worker,
                "record_worker_started",
                return_value=reference,
            ) as record_started,
            mock.patch.object(i_outbound, "send_progress"),
        ):
            result = m_manager_tools_registry.execute_single_tool(spec, "carbon-a")

        record_started.assert_called_once_with(
            "carbon-a",
            "builder",
            "terminal",
            "Build the app.",
            queued=False,
            task_id="",
        )
        self.assertIn("task_id=task-fitness", result)
        self.assertIn("group_id=group-build", result)
        self.assertIn("invocation_id=invocation-builder-1", result)

    def test_message_manager_returns_the_local_automatic_call_identity(self):
        spec = {
            "tool": "message_manager",
            "carbon_id": "carbon-b",
            "message": "Can you review this?",
        }
        work_call = {
            "owner_contact_id": "carbon-a",
            "task_id": "task-fitness",
            "call_id": "call-review",
        }

        with (
            mock.patch.object(
                m_manager_tools_messaging,
                "ensure_contact_for_target",
                return_value={
                    "carbon_id": "carbon-b",
                    "display_name": "B",
                },
            ),
            mock.patch.object(
                m_manager_tools_messaging,
                "prepare_outbound_call",
                return_value=work_call,
            ),
            mock.patch.object(
                m_manager_tools_messaging,
                "send_manager_message",
                return_value="Done. queued",
            ) as send_manager_message,
            mock.patch.object(i_outbound, "send_progress"),
        ):
            result = m_manager_tools_registry.execute_single_tool(spec, "carbon-a")

        send_manager_message.assert_called_once_with(
            "carbon-a",
            "carbon-b",
            "Can you review this?",
            target_type="carbon",
            work_call=work_call,
        )
        self.assertIn("task_id=task-fitness", result)
        self.assertIn("call_id=call-review", result)

    def test_message_manager_returns_identity_after_durable_queue_acceptance(self):
        work_call = {
            "owner_contact_id": "carbon-a",
            "task_id": "task-fitness",
            "work_event_id": "event-review",
            "call_id": "call-review",
        }
        targets = (
            (
                {"carbon_id": "carbon-b"},
                {
                    "contact_type": "carbon",
                    "carbon_id": "carbon-b",
                    "display_name": "B",
                },
            ),
            (
                {"silicon_id": "silicon-b"},
                {
                    "contact_type": "silicon",
                    "silicon_id": "silicon-b",
                    "display_name": "Builder",
                },
            ),
        )

        for target, contact in targets:
            with self.subTest(target=target):
                spec = {
                    "tool": "message_manager",
                    **target,
                    "message": "Can you review this?",
                }
                with (
                    mock.patch.object(
                        m_manager_tools_messaging,
                        "ensure_contact_for_target",
                        return_value=contact,
                    ),
                    mock.patch.object(
                        m_manager_tools_messaging,
                        "prepare_outbound_call",
                        return_value=work_call,
                    ),
                    mock.patch.object(
                        m_manager_tools_messaging,
                        "send_manager_message",
                        return_value="Done. queued",
                    ) as send_manager_message,
                    mock.patch.object(i_outbound, "send_progress"),
                ):
                    result = m_manager_tools_registry.execute_single_tool(spec, "carbon-a")

                send_manager_message.assert_called_once()
                self.assertIn("Done. queued", result)
                self.assertIn("Work update:", result)
                self.assertIn("task_id=task-fitness", result)
                self.assertIn("work_event_id=event-review", result)
                self.assertIn("call_id=call-review", result)

    def test_message_manager_preparation_failure_sends_nothing_and_is_body_free(
        self,
    ):
        targets = (
            (
                {"carbon_id": "carbon-b"},
                {
                    "contact_type": "carbon",
                    "carbon_id": "carbon-b",
                    "display_name": "B",
                },
            ),
            (
                {"silicon_id": "silicon-b"},
                {
                    "contact_type": "silicon",
                    "silicon_id": "silicon-b",
                    "display_name": "Builder",
                },
            ),
        )

        for target, contact in targets:
            with self.subTest(target=target):
                spec = {
                    "tool": "message_manager",
                    **target,
                    "message": "Private manager message.",
                }
                with (
                    mock.patch.object(
                        m_manager_tools_messaging,
                        "ensure_contact_for_target",
                        return_value=contact,
                    ),
                    mock.patch.object(
                        m_manager_tools_messaging,
                        "prepare_outbound_call",
                        side_effect=OSError(
                            "disk failed while handling Private manager message."
                        ),
                    ),
                    mock.patch.object(
                        m_manager_tools_messaging,
                        "send_manager_message",
                    ) as send_manager_message,
                    mock.patch(
                        "interface.work.enqueue_outbound_call",
                    ) as enqueue_outbound_call,
                    mock.patch.object(i_outbound, "send_progress"),
                ):
                    result = m_manager_tools_registry.execute_single_tool(spec, "carbon-a")

                send_manager_message.assert_not_called()
                enqueue_outbound_call.assert_not_called()
                self.assertIn("Message not sent", result)
                self.assertIn("(OSError)", result)
                self.assertNotIn("Private manager message", result)
                self.assertNotIn("disk failed", result)

    def test_message_manager_failure_reports_progress_and_output(self):
        spec = {
            "tool": "message_manager",
            "carbon_id": "missing-carbon",
            "message": "hello",
        }

        with (
            mock.patch.object(m_manager_tools_messaging, "ensure_contact_for_target", side_effect=Exception("api 404: Target not found.")),
            mock.patch.object(m_manager_tools_messaging, "send_manager_message") as send_manager_message,
            mock.patch.object(i_outbound, "send_progress") as send_progress,
        ):
            result = m_manager_tools_registry.execute_single_tool(spec, "carbon-a")

        send_manager_message.assert_not_called()
        self.assertIn("Message failed: carbon 'missing-carbon' could not be reached.", result)
        self.assertIn("api 404: Target not found.", result)
        self.assertTrue(
            any(
                call.args
                == (
                    "carbon-a",
                    "manager:carbon-a",
                    "calling",
                    "Message failed: carbon 'missing-carbon' could not be reached. api 404: Target not found.",
                )
                for call in send_progress.call_args_list
            )
        )

    def test_remote_browser_share_passes_start_url(self):
        spec = {
            "tool": "remote_browser",
            "type": "share",
            "expiry": 120,
            "new": True,
            "url": "https://example.com/login",
        }

        with (
            mock.patch.object(m_manager_tools_browser, "remote_browser_share", return_value="Done. shared") as share,
            mock.patch.object(i_outbound, "send_progress") as send_progress,
        ):
            result = m_manager_tools_registry.execute_single_tool(spec, "carbon-a")

        share.assert_called_once_with(
            "carbon-a",
            expiry=120,
            new=True,
            url="https://example.com/login",
        )
        send_progress.assert_called()
        self.assertEqual(result, "Tool 'remote_browser/share': Done. shared")

    def test_advertising_memory_update_uses_own_fixed_memory(self):
        spec = {
            "tool": "advertising_memory/update",
            "content": "# Current work\n- Reviewing the release",
        }
        outcome = {
            "ok": True,
            "status": "uploaded",
            "revision": 3,
        }

        with (
            mock.patch(
                "interface.team.publish.update_own_advertising_memory",
                return_value=outcome,
            ) as update_memory,
            mock.patch.object(
                m_manager_tools_memory,
                "acknowledge_team_context_result",
            ) as acknowledge,
            mock.patch.object(i_outbound, "send_progress") as send_progress,
        ):
            result = m_manager_tools_registry.execute_single_tool(spec, "carbon-a")

        update_memory.assert_called_once_with(
            spec["content"],
            root=str(PROJECT_ROOT),
            resolve_conflict=False,
        )
        send_progress.assert_called_with(
            "carbon-a",
            "manager:carbon-a",
            "executing",
            "updating team-visible advertising memory",
        )
        acknowledge.assert_called_once_with(outcome)
        self.assertEqual(
            result,
            "Tool 'advertising_memory/update': uploaded — revision 3",
        )

    def test_advertising_memory_conflict_can_be_explicitly_resolved(self):
        spec = {
            "tool": "advertising_memory/update",
            "content": "# Current work\n- Resolved snapshot",
            "resolve_conflict": True,
        }
        with (
            mock.patch(
                "interface.team.publish.update_own_advertising_memory",
                return_value={
                    "ok": True,
                    "status": "uploaded",
                    "revision": 8,
                },
            ) as update_memory,
            mock.patch.object(i_outbound, "send_progress"),
        ):
            result = m_manager_tools_registry.execute_single_tool(spec, "carbon-a")

        update_memory.assert_called_once_with(
            spec["content"],
            root=str(PROJECT_ROOT),
            resolve_conflict=True,
        )
        self.assertEqual(
            result,
            "Tool 'advertising_memory/update': uploaded — revision 8",
        )

    def test_advertising_memory_failure_surfaces_draft_and_revision(self):
        with (
            mock.patch(
                "interface.team.publish.update_own_advertising_memory",
                return_value={
                    "ok": False,
                    "status": "conflict",
                    "local_saved": True,
                    "actual_revision": 7,
                },
            ),
            mock.patch.object(i_outbound, "send_progress"),
        ):
            result = m_manager_tools_registry.execute_single_tool(
                {
                    "tool": "advertising_memory/update",
                    "content": "# Current work\n- Local draft",
                },
                "carbon-a",
            )

        self.assertEqual(
            result,
            "Tool 'advertising_memory/update': Error: conflict — "
            "Glass is at revision 7; local draft preserved",
        )

    def test_advertising_memory_update_requires_string_content(self):
        result = m_manager_tools_registry.execute_single_tool(
            {"tool": "advertising_memory/update"},
            "carbon-a",
        )

        self.assertEqual(
            result,
            "Tool 'advertising_memory/update': Error: content must be a string",
        )

        invalid_resolution = m_manager_tools_registry.execute_single_tool(
            {
                "tool": "advertising_memory/update",
                "content": "",
                "resolve_conflict": "yes",
            },
            "carbon-a",
        )
        self.assertEqual(
            invalid_resolution,
            "Tool 'advertising_memory/update': Error: "
            "resolve_conflict must be a boolean",
        )

    def test_advertising_memory_content_is_redacted_from_process_log(self):
        content = "private operational snapshot"
        output = json.dumps(
            {
                "tools": [
                    {
                        "tool": "advertising_memory/update",
                        "content": content,
                    }
                ]
            }
        )

        rendered = m_manager_tools_registry._manager_output_for_log(output, json.loads(output))

        self.assertEqual(rendered, "[Private tool invocation omitted]")
        self.assertNotIn(content, rendered)

    def test_incomplete_escaped_advertising_tool_is_redacted_from_process_log(self):
        content = "private truncated payload"
        output = (
            '{"tools":[{"tool":"advertising_memory\\u002fupdate",'
            f'"content":"{content}"'
        )

        rendered = m_manager_tools_registry._manager_output_for_log(output, {})

        self.assertEqual(rendered, "[Private tool invocation omitted]")
        self.assertNotIn(content, rendered)

    def test_codex_stream_redacts_advertising_file_output_by_item_id(self):
        private_items = set()
        started = redact_agent_message(
            json.dumps(
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "id": "read-ad",
                            "type": "commandExecution",
                            "command": "cat prompts/advertising/peer.md",
                        }
                    },
                }
            ),
            private_items,
        )
        delta = redact_agent_message(
            json.dumps(
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "itemId": "read-ad",
                        "delta": "PEER ADVERTISING CONTENT",
                    },
                }
            ),
            private_items,
        )

        self.assertIn('"redacted":true', started)
        self.assertIn('"redacted":true', delta)
        self.assertNotIn("PEER ADVERTISING CONTENT", delta)

    def test_codex_stream_redacts_all_shell_commands_and_output(self):
        private_items = set()
        started = redact_agent_message(
            json.dumps(
                {
                    "method": "item/started",
                    "params": {
                        "item": {
                            "id": "shell-command",
                            "type": "commandExecution",
                            "command": "cat prompts/a[d]vertising/peer.md",
                        }
                    },
                }
            ),
            private_items,
        )
        delta = redact_agent_message(
            json.dumps(
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "itemId": "shell-command",
                        "delta": "PEER ADVERTISING CONTENT",
                    },
                }
            ),
            private_items,
        )

        self.assertIn('"redacted":true', started)
        self.assertIn('"redacted":true', delta)
        self.assertNotIn("a[d]vertising", started)
        self.assertNotIn("PEER ADVERTISING CONTENT", delta)

    def test_advertising_file_reference_redacts_final_manager_process_log(self):
        output = (
            "Read prompts/advertising/peer.md: "
            "PEER ADVERTISING CONTENT"
        )

        rendered = m_manager_tools_registry._manager_output_for_log(output, {})

        self.assertEqual(rendered, "[Advertising memory content omitted]")
        self.assertNotIn("PEER ADVERTISING CONTENT", rendered)

    def test_manager_process_log_redacts_provider_credentials(self):
        rendered = m_manager_tools_registry._manager_output_for_log(
            "provider failed: api_key=scs_live_supersecret",
            {},
        )

        self.assertNotIn("scs_live_supersecret", rendered)
        self.assertIn("redacted", rendered)

    def test_advertising_memory_content_is_redacted_from_activity_log(self):
        content = "team-visible but not durable-log content"
        with mock.patch.object(activity_log, "log") as write_log:
            activity_log.tool_call(
                "carbon-a",
                "advertising_memory/update",
                {
                    "tool": "advertising_memory/update",
                    "content": content,
                    "backup": content,
                },
                "uploaded",
            )

        _, _, kwargs = write_log.mock_calls[0]
        self.assertNotIn(content, repr(kwargs))
        self.assertEqual(kwargs["args"], {})
        self.assertEqual(
            kwargs["result"],
            "[Advertising memory result omitted]",
        )

        with mock.patch.object(activity_log, "log") as write_log:
            activity_log.tool_call(
                "carbon-a",
                "advertising_memory/update",
                content,
                content,
            )

        _, _, kwargs = write_log.mock_calls[0]
        self.assertEqual(kwargs["args"], {})
        self.assertNotIn(content, repr(kwargs))

    def test_valid_private_tool_with_rate_limit_words_is_not_logged_as_failure(self):
        content = "AD_PAYLOAD rate limit work"
        output = json.dumps({
            "tools": [{
                "tool": "advertising_memory/update",
                "content": content,
            }]
        })

        with (
            mock.patch.object(manager.INFERENCE, "_order", ["claude"]),
            mock.patch.object(
                inference.get_provider("claude"),
                "run_turn",
                return_value=inference.TurnResult(output, output, []),
            ),
            mock.patch("prompts.loader.get_manager_prompt", return_value="sp"),
            mock.patch("builtins.print") as printed,
        ):
            result = manager.manager_code("update it", "carbon-private")

        self.assertEqual(result[0], output)
        rendered = " ".join(
            str(arg)
            for call in printed.call_args_list
            for arg in call.args
        )
        self.assertNotIn(content, rendered)

    def test_streaming_provider_stderr_is_never_returned_or_printed(self):
        secret = "private advertising text from provider stderr"

        class Input:
            def write(self, _value):
                return None

            def close(self):
                return None

        class Output:
            def readline(self):
                return ""

        class Error:
            def read(self):
                return secret

        class Process:
            stdin = Input()
            stdout = Output()
            stderr = Error()

            def wait(self):
                return 1

        with (
            mock.patch.object(claude_stream.subprocess, "Popen", return_value=Process()),
            mock.patch("builtins.print") as printed,
        ):
            result = claude_stream.run_streaming(
                ["provider"],
                "",
                "manager:private",
                cwd=None,
            )

        self.assertEqual(result.stderr, "[provider stderr omitted]")
        rendered = " ".join(
            str(arg)
            for call in printed.call_args_list
            for arg in call.args
        )
        self.assertNotIn(secret, rendered)

    def test_private_provider_exception_is_not_echoed_to_the_carbon(self):
        secret = (
            '{"tools":[{"tool":"advertising_memory/update",'
            '"content":"PRIVATE_EXCEPTION_ECHO"}]}'
        )

        rendered = inference.error_tools(RuntimeError(secret))

        self.assertNotIn("PRIVATE_EXCEPTION_ECHO", rendered)
        self.assertIn("provider call failed", rendered)

    def test_malformed_private_rate_limit_output_gets_generic_carbon_reply(self):
        content = "TEAM_ONLY rate limit"
        output = (
            '{"tools":[{"tool":"advertising_memory/update",'
            f'"content":"{content}'
        )

        rendered = m_manager_tools_registry._rate_limit_reply_text(output)

        self.assertEqual(
            rendered,
            "The manager provider is rate-limited. Please try again shortly.",
        )
        self.assertNotIn(content, rendered)

    def test_private_tool_diagnostics_omit_every_model_supplied_argument(self):
        payload = "AD_PAYLOAD duplicated through alias fields"
        for tool_name in (
            "advertising_memory/update",
            "work_update",
            "trust/list",
            "trust/get",
            "trust/set",
        ):
            with self.subTest(tool=tool_name):
                self.assertEqual(
                    m_manager_tools_registry._diagnostic_tool_metadata({
                        "tool": tool_name,
                        "content": payload,
                        "type": payload,
                        "worker-id": payload,
                        "silicon_id": payload,
                        "carbon_id": payload,
                    }),
                    {"tool": tool_name},
                )

        trace = m_manager_turn.Diagnostics.start_run(
            "message",
            "carbon-private-diagnostics",
            base_dir=tempfile.mkdtemp(prefix="private-tool-diag-"),
        )
        m_manager_turn.Diagnostics.register_active("carbon-private-diagnostics", trace)
        self.addCleanup(
            m_manager_turn.Diagnostics.unregister_active,
            "carbon-private-diagnostics",
            trace,
        )
        with (
            mock.patch(
                "interface.team.publish.update_own_advertising_memory",
                return_value={"ok": True, "status": "uploaded", "revision": 1},
            ),
            mock.patch.object(i_outbound, "send_progress"),
        ):
            m_manager_tools_registry.execute_single_tool(
                {
                    "tool": "advertising_memory/update",
                    "content": payload,
                    "type": payload,
                    "worker-id": payload,
                    "silicon_id": payload,
                    "carbon_id": payload,
                },
                "carbon-private-diagnostics",
            )

        span = next(item for item in trace.spans if item.name == "tool_call")
        self.assertNotIn(payload, json.dumps(span.meta))
        self.assertEqual(span.meta["tool"], "advertising_memory/update")
        self.assertNotIn("action", span.meta)
        self.assertNotIn("worker_id", span.meta)
        self.assertNotIn("target_id", span.meta)


if __name__ == "__main__":
    unittest.main()
