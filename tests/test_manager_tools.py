import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
import manager
from core import activity_log


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ManagerToolsDocTest(unittest.TestCase):
    def test_json_examples_are_valid(self):
        text = (PROJECT_ROOT / "prompts" / "MANAGER_TOOLS.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)\n```", text, flags=re.S)
        self.assertGreater(len(blocks), 0)
        for index, block in enumerate(blocks, 1):
            with self.subTest(block=index):
                json.loads(block)


class ManagerToolExecutionTest(unittest.TestCase):
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
        missing_id = main.execute_single_tool(
            {"tool": "worker/browser", "type": "new", "task": "research"},
            "carbon-a",
        )
        self.assertEqual(missing_id, "Tool 'worker/new': Error: worker-id is required")

        missing_task = main.execute_single_tool(
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
                "core.trust.inspect_trust_policy",
                return_value=snapshot,
            ) as inspect,
            mock.patch.object(main, "send_progress"),
        ):
            result = main.execute_single_tool(
                {"tool": "trust/list"},
                "carbon-a",
            )

        inspect.assert_called_once_with(
            kind="",
            public_id="",
            root=main.PROJECT_ROOT,
            refresh=True,
        )
        self.assertIn('"revision": "7:2"', result)
        self.assertIn('"id": "peer-si"', result)

    def test_trust_get_requires_one_typed_identity(self):
        result = main.execute_single_tool(
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
            mock.patch.object(main, "start_worker", return_value="Done. started") as start_worker,
            mock.patch.object(main, "add_checkback") as add_checkback,
            mock.patch.object(main, "send_progress") as send_progress,
        ):
            result = main.execute_single_tool(spec, "carbon-a")

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
            mock.patch.object(main, "start_worker", return_value="Done. started"),
            mock.patch.object(
                main,
                "record_worker_started",
                return_value=reference,
            ) as record_started,
            mock.patch.object(main, "send_progress"),
        ):
            result = main.execute_single_tool(spec, "carbon-a")

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
                main,
                "ensure_contact_for_target",
                return_value={
                    "carbon_id": "carbon-b",
                    "display_name": "B",
                },
            ),
            mock.patch.object(
                main,
                "prepare_outbound_call",
                return_value=work_call,
            ),
            mock.patch.object(main, "enqueue_outbound_call") as enqueue_outbound,
            mock.patch.object(
                main,
                "send_manager_message",
                return_value="Done. queued",
            ) as send_manager_message,
            mock.patch.object(main, "send_progress"),
        ):
            result = main.execute_single_tool(spec, "carbon-a")

        send_manager_message.assert_called_once_with(
            "carbon-a",
            "carbon-b",
            "Can you review this?",
            target_type="carbon",
            work_call=work_call,
        )
        enqueue_outbound.assert_called_once_with(
            work_call,
            target_name="B's manager",
            message="Can you review this?",
        )
        self.assertIn("task_id=task-fitness", result)
        self.assertIn("call_id=call-review", result)

    def test_message_manager_failure_reports_progress_and_output(self):
        spec = {
            "tool": "message_manager",
            "carbon_id": "missing-carbon",
            "message": "hello",
        }

        with (
            mock.patch.object(main, "ensure_contact_for_target", side_effect=Exception("api 404: Target not found.")),
            mock.patch.object(main, "send_manager_message") as send_manager_message,
            mock.patch.object(main, "send_progress") as send_progress,
        ):
            result = main.execute_single_tool(spec, "carbon-a")

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
            mock.patch.object(main, "remote_browser_share", return_value="Done. shared") as share,
            mock.patch.object(main, "send_progress") as send_progress,
        ):
            result = main.execute_single_tool(spec, "carbon-a")

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

        with (
            mock.patch(
                "core.team_context.update_own_advertising_memory",
                return_value={
                    "ok": True,
                    "status": "uploaded",
                    "revision": 3,
                },
            ) as update_memory,
            mock.patch.object(main, "send_progress") as send_progress,
        ):
            result = main.execute_single_tool(spec, "carbon-a")

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
                "core.team_context.update_own_advertising_memory",
                return_value={
                    "ok": True,
                    "status": "uploaded",
                    "revision": 8,
                },
            ) as update_memory,
            mock.patch.object(main, "send_progress"),
        ):
            result = main.execute_single_tool(spec, "carbon-a")

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
                "core.team_context.update_own_advertising_memory",
                return_value={
                    "ok": False,
                    "status": "conflict",
                    "local_saved": True,
                    "actual_revision": 7,
                },
            ),
            mock.patch.object(main, "send_progress"),
        ):
            result = main.execute_single_tool(
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
        result = main.execute_single_tool(
            {"tool": "advertising_memory/update"},
            "carbon-a",
        )

        self.assertEqual(
            result,
            "Tool 'advertising_memory/update': Error: content must be a string",
        )

        invalid_resolution = main.execute_single_tool(
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

        rendered = main._manager_output_for_log(output, json.loads(output))

        self.assertEqual(rendered, "[Private tool invocation omitted]")
        self.assertNotIn(content, rendered)

    def test_incomplete_escaped_advertising_tool_is_redacted_from_process_log(self):
        content = "private truncated payload"
        output = (
            '{"tools":[{"tool":"advertising_memory\\u002fupdate",'
            f'"content":"{content}"'
        )

        rendered = main._manager_output_for_log(output, {})

        self.assertEqual(rendered, "[Private tool invocation omitted]")
        self.assertNotIn(content, rendered)

    def test_codex_stream_redacts_advertising_file_output_by_item_id(self):
        private_items = set()
        started = manager._redact_codex_agent_message(
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
        delta = manager._redact_codex_agent_message(
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
        started = manager._redact_codex_agent_message(
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
        delta = manager._redact_codex_agent_message(
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

        rendered = main._manager_output_for_log(output, {})

        self.assertEqual(rendered, "[Advertising memory content omitted]")
        self.assertNotIn("PEER ADVERTISING CONTENT", rendered)

    def test_manager_process_log_redacts_provider_credentials(self):
        rendered = main._manager_output_for_log(
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
            mock.patch.object(manager, "get_brain_order", return_value=["claude"]),
            mock.patch.object(
                manager,
                "claude_code",
                return_value=(output, output, []),
            ),
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
            mock.patch.object(manager.subprocess, "Popen", return_value=Process()),
            mock.patch("builtins.print") as printed,
        ):
            result = manager._run_streaming(
                ["provider"],
                "",
                "manager:private",
            )

        self.assertEqual(result[4], "[provider stderr omitted]")
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

        rendered = manager._safe_manager_error_tools(RuntimeError(secret))

        self.assertNotIn("PRIVATE_EXCEPTION_ECHO", rendered)
        self.assertIn("provider call failed", rendered)

    def test_malformed_private_rate_limit_output_gets_generic_carbon_reply(self):
        content = "TEAM_ONLY rate limit"
        output = (
            '{"tools":[{"tool":"advertising_memory/update",'
            f'"content":"{content}'
        )

        rendered = main._rate_limit_reply_text(output)

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
            "extend",
            "extend/request_setup",
        ):
            with self.subTest(tool=tool_name):
                self.assertEqual(
                    main._diagnostic_tool_metadata({
                        "tool": tool_name,
                        "content": payload,
                        "type": payload,
                        "worker-id": payload,
                        "silicon_id": payload,
                        "carbon_id": payload,
                    }),
                    {"tool": tool_name},
                )

        trace = main.Diagnostics.start_run(
            "message",
            "carbon-private-diagnostics",
            base_dir=tempfile.mkdtemp(prefix="private-tool-diag-"),
        )
        main.Diagnostics.register_active("carbon-private-diagnostics", trace)
        self.addCleanup(
            main.Diagnostics.unregister_active,
            "carbon-private-diagnostics",
            trace,
        )
        with (
            mock.patch(
                "core.team_context.update_own_advertising_memory",
                return_value={"ok": True, "status": "uploaded", "revision": 1},
            ),
            mock.patch.object(main, "send_progress"),
        ):
            main.execute_single_tool(
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
