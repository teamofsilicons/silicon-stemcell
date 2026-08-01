import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker import extend_cli, handler


class WorkerExtendCliTest(unittest.TestCase):
    def invoke(self, action, body=None, *, environ=None):
        stdin = io.BytesIO(
            b"" if body is None else json.dumps(body).encode("utf-8")
        )
        stdout = io.StringIO()
        code = extend_cli.run(
            [action],
            stdin=stdin,
            stdout=stdout,
            environ=(
                {extend_cli.CONTACT_ENV: "contact-a"}
                if environ is None
                else environ
            ),
        )
        rendered = stdout.getvalue()
        return code, json.loads(rendered), rendered

    def test_list_projects_only_public_invocation_fields(self):
        credential = "provider-credential-must-not-print"
        with mock.patch.object(
            extend_cli.extend,
            "load_directory",
            return_value={
                "tools": [
                    {
                        "key": "gmail.messages.send",
                        "name": "Send message",
                        "description": "Send a Gmail message.",
                        "setup_status": "ready",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "to": {
                                    "type": "string",
                                    "default": credential,
                                },
                                "api_key": {
                                    "type": "string",
                                    "default": credential,
                                },
                            },
                        },
                        "config": {"api_key": credential},
                        "credentials": {"token": credential},
                        "provider": {"account": credential},
                        "integration": {"connect_url": credential},
                    }
                ]
            },
        ) as load_directory:
            code, payload, rendered = self.invoke("list")

        self.assertEqual(code, extend_cli.EXIT_OK)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tools"][0]["key"], "gmail.messages.send")
        self.assertNotIn(credential, rendered)
        self.assertNotIn('"config"', rendered)
        self.assertNotIn('"credentials"', rendered)
        self.assertNotIn('"provider"', rendered)
        self.assertNotIn('"integration"', rendered)
        self.assertNotIn('"default"', rendered)
        self.assertIn("api_key", rendered)
        load_directory.assert_called_once_with(
            force=True,
            strict=True,
        )

    def test_list_reports_directory_outage_instead_of_empty_catalog(self):
        with mock.patch.object(
            extend_cli.extend,
            "load_directory",
            side_effect=extend_cli.extend.ExtendError(
                "unreachable",
                code="EXTEND_UNREACHABLE",
            ),
        ):
            code, payload, _rendered = self.invoke("list")

        self.assertEqual(code, extend_cli.EXIT_EXTEND)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "extend_unreachable")

    def test_list_marks_a_catalog_larger_than_the_output_projection(self):
        tools = [
            {
                "key": f"tool.{index}",
                "name": f"Tool {index}",
                "input_schema": {"type": "object"},
            }
            for index in range(extend_cli.MAX_COLLECTION_ITEMS + 1)
        ]
        with mock.patch.object(
            extend_cli.extend,
            "load_directory",
            return_value={"tools": tools},
        ):
            code, payload, _rendered = self.invoke("list")

        self.assertEqual(code, extend_cli.EXIT_OK)
        self.assertTrue(payload["truncated"])
        self.assertEqual(
            len(payload["tools"]),
            extend_cli.MAX_COLLECTION_ITEMS,
        )

    def test_execute_uses_inherited_contact_and_stdin_arguments(self):
        with mock.patch.object(
            extend_cli.extend,
            "execute_tool_result",
            return_value={
                "tool": "dope.dopes.create",
                "setup_requested": False,
                "result": {"doping_id": "DP1", "title": "Launch"},
            },
        ) as execute:
            code, payload, _rendered = self.invoke(
                "execute",
                {
                    "tool": "dope.dopes.create",
                    "arguments": {"title": "Launch"},
                },
                environ={extend_cli.CONTACT_ENV: "originating-contact"},
            )

        self.assertEqual(code, extend_cli.EXIT_OK)
        self.assertEqual(payload["result"]["doping_id"], "DP1")
        execute.assert_called_once_with(
            "dope.dopes.create",
            {"title": "Launch"},
            carbon_id="originating-contact",
        )

    def test_execute_drops_credentials_but_keeps_legitimate_result_fields(self):
        credential = "not-a-recognizable-prefix-but-still-private"
        with mock.patch.object(
            extend_cli.extend,
            "execute_tool_result",
            return_value={
                "tool": "gmail.messages.get",
                "setup_requested": False,
                "result": {
                    "message_id": "M1",
                    "access_token": credential,
                    "key": "message-key",
                    "headers": {"subject": "Visible header"},
                    "provider": "gmail",
                    "connection": "connected",
                    "config": {"region": "us-east-1"},
                    "nested": {
                        "client_secret": credential,
                        "subject": "Visible",
                    },
                },
            },
        ):
            code, payload, rendered = self.invoke(
                "execute",
                {"tool": "gmail.messages.get", "arguments": {"id": "M1"}},
            )

        self.assertEqual(code, extend_cli.EXIT_OK)
        self.assertEqual(payload["result"]["message_id"], "M1")
        self.assertEqual(payload["result"]["nested"]["subject"], "Visible")
        self.assertEqual(payload["result"]["key"], "message-key")
        self.assertEqual(payload["result"]["headers"]["subject"], "Visible header")
        self.assertEqual(payload["result"]["provider"], "gmail")
        self.assertEqual(payload["result"]["connection"], "connected")
        self.assertEqual(payload["result"]["config"]["region"], "us-east-1")
        self.assertNotIn(credential, rendered)
        self.assertNotIn("access_token", rendered)
        self.assertNotIn("client_secret", rendered)

    def test_successful_result_text_containing_error_marker_stays_successful(self):
        with mock.patch.object(
            extend_cli.extend,
            "execute_tool_result",
            return_value={
                "tool": "gmail.messages.get",
                "setup_requested": False,
                "result": {"subject": "Status: Error: already resolved"},
            },
        ):
            code, payload, _rendered = self.invoke(
                "execute",
                {"tool": "gmail.messages.get", "arguments": {"id": "M1"}},
            )

        self.assertEqual(code, extend_cli.EXIT_OK)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["result"]["subject"],
            "Status: Error: already resolved",
        )

    def test_request_setup_reads_note_from_stdin_without_echoing_it(self):
        note = "private reason for this Carbon"
        with mock.patch.object(
            extend_cli.extend,
            "request_setup_result",
            return_value={
                "tool": "gmail.messages.send",
                "setup_requested": True,
                "request_id": "REQ1",
            },
        ) as request:
            code, payload, rendered = self.invoke(
                "request-setup",
                {"tool": "gmail.messages.send", "note": note},
            )

        self.assertEqual(code, extend_cli.EXIT_OK)
        self.assertEqual(payload["tool"], "gmail.messages.send")
        self.assertNotIn(note, rendered)
        request.assert_called_once_with(
            "gmail.messages.send",
            note=note,
            carbon_id="contact-a",
        )

    def test_stdin_or_argv_cannot_override_acting_contact(self):
        stdout = io.StringIO()
        with mock.patch.object(extend_cli.extend, "execute_tool_result") as execute:
            code = extend_cli.run(
                ["execute"],
                stdin=io.BytesIO(
                    json.dumps(
                        {
                            "tool": "gmail.messages.get",
                            "arguments": {},
                            "carbon_id": "different-carbon",
                        }
                    ).encode("utf-8")
                ),
                stdout=stdout,
                environ={extend_cli.CONTACT_ENV: "originating-contact"},
            )

        self.assertEqual(code, extend_cli.EXIT_INPUT)
        self.assertNotIn("different-carbon", stdout.getvalue())
        execute.assert_not_called()

        stdout = io.StringIO()
        code = extend_cli.run(
            ["execute", "different-carbon"],
            stdin=io.BytesIO(b"{}"),
            stdout=stdout,
            environ={extend_cli.CONTACT_ENV: "originating-contact"},
        )
        self.assertEqual(code, extend_cli.EXIT_INPUT)
        self.assertNotIn("different-carbon", stdout.getvalue())

    def test_execute_requires_worker_context(self):
        with mock.patch.object(extend_cli.extend, "execute_tool_result") as execute:
            code, payload, _rendered = self.invoke(
                "execute",
                {"tool": "gmail.messages.get", "arguments": {}},
                environ={},
            )

        self.assertEqual(code, extend_cli.EXIT_INPUT)
        self.assertEqual(payload["error"]["code"], "context_missing")
        execute.assert_not_called()

    def test_extend_error_is_generic_and_does_not_echo_provider_secret(self):
        credential = "ak_this_must_never_print"
        with mock.patch.object(
            extend_cli.extend,
            "execute_tool_result",
            side_effect=extend_cli.extend.ExtendError(
                f"provider returned {credential}",
                code=credential,
            ),
        ):
            code, payload, rendered = self.invoke(
                "execute",
                {"tool": "gmail.messages.get", "arguments": {}},
            )

        self.assertEqual(code, extend_cli.EXIT_EXTEND)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "extend_error")
        self.assertNotIn(credential, rendered)
        self.assertNotIn("provider returned", rendered)

    def test_output_is_bounded_and_remains_valid_json(self):
        result = {
            f"field_{index}": "x" * extend_cli.MAX_RESULT_STRING
            for index in range(100)
        }
        with mock.patch.object(
            extend_cli.extend,
            "execute_tool_result",
            return_value={
                "tool": "dope.bulk",
                "setup_requested": False,
                "result": result,
            },
        ):
            code, payload, rendered = self.invoke(
                "execute",
                {"tool": "dope.bulk", "arguments": {}},
            )

        self.assertEqual(code, extend_cli.EXIT_OK)
        self.assertTrue(payload["truncated"])
        self.assertLessEqual(
            len(rendered.rstrip("\n").encode("utf-8")),
            extend_cli.MAX_OUTPUT_BYTES,
        )

    def test_string_truncation_is_explicit(self):
        with mock.patch.object(
            extend_cli.extend,
            "execute_tool_result",
            return_value={
                "tool": "dope.large",
                "setup_requested": False,
                "result": {"body": "x" * (extend_cli.MAX_RESULT_STRING + 1)},
            },
        ):
            code, payload, _rendered = self.invoke(
                "execute",
                {"tool": "dope.large", "arguments": {}},
            )

        self.assertEqual(code, extend_cli.EXIT_OK)
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["result"]["body"].endswith("…"))

    def test_input_is_bounded_without_echoing_it(self):
        private = "private-input-value"
        stdout = io.StringIO()
        code = extend_cli.run(
            ["execute"],
            stdin=io.BytesIO(
                json.dumps(
                    {
                        "tool": "dope.bulk",
                        "arguments": {"body": private * 20_000},
                    }
                ).encode("utf-8")
            ),
            stdout=stdout,
            environ={extend_cli.CONTACT_ENV: "contact-a"},
        )

        self.assertEqual(code, extend_cli.EXIT_INPUT)
        self.assertNotIn(private, stdout.getvalue())

    def test_unexpected_exception_is_not_returned(self):
        credential = "ck_unexpected_exception_secret"
        with mock.patch.object(
            extend_cli.extend,
            "execute_tool_result",
            side_effect=RuntimeError(credential),
        ):
            code, payload, rendered = self.invoke(
                "execute",
                {"tool": "gmail.messages.get", "arguments": {}},
            )

        self.assertEqual(code, extend_cli.EXIT_INTERNAL)
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertNotIn(credential, rendered)


class WorkerExtendContextLaunchTest(unittest.TestCase):
    class FakeProcess:
        pid = 4321

        def __init__(self):
            self.stdin = io.StringIO()

    def test_worker_environment_overwrites_stale_contact(self):
        with (
            mock.patch.dict(
                os.environ,
                {
                    handler.EXTEND_CONTACT_ENV: "stale-contact",
                    handler.EXTEND_ACTING_CARBON_ENV: "stale-carbon",
                    handler.EXTEND_ROOM_ENV: "stale-room",
                },
                clear=False,
            ),
            mock.patch(
                "core.interface.get_contact",
                return_value={
                    "contact_type": "carbon",
                    "carbon_id": "carbon-1",
                    "room_id": "ROOM1",
                },
            ),
        ):
            env = handler._worker_process_env("current-contact")
            self.assertEqual(env[handler.EXTEND_CONTACT_ENV], "carbon-1")
            self.assertEqual(
                env[handler.EXTEND_ACTING_CARBON_ENV],
                "carbon-1",
            )
            self.assertEqual(env[handler.EXTEND_ROOM_ENV], "ROOM1")
            self.assertEqual(
                os.environ[handler.EXTEND_CONTACT_ENV],
                "stale-contact",
            )
            self.assertEqual(
                os.environ[handler.EXTEND_ACTING_CARBON_ENV],
                "stale-carbon",
            )
            self.assertEqual(
                os.environ[handler.EXTEND_ROOM_ENV],
                "stale-room",
            )

    def test_resumed_claude_worker_inherits_contact_outside_command_line(self):
        process = self.FakeProcess()
        with tempfile.TemporaryDirectory() as temp:
            output_path = str(Path(temp) / "claude-output.jsonl")
            with (
                mock.patch.object(
                    handler,
                    "_get_worker_record",
                    return_value={"session_id": "claude-session"},
                ),
                mock.patch.object(
                    handler,
                    "get_worker_prompt",
                    return_value=("worker prompt", ""),
                ),
                mock.patch.object(
                    handler,
                    "_run_output_path",
                    return_value=output_path,
                ),
                mock.patch.object(
                    handler.subprocess,
                    "Popen",
                    return_value=process,
                ) as popen,
                mock.patch.object(handler, "_record_active_run"),
            ):
                ok, _result = handler._launch_claude_worker_process(
                    "worker-a",
                    "resume task",
                    "terminal",
                    "origin-contact",
                    resume=True,
                    session_id="claude-session",
                )

        self.assertTrue(ok)
        command = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        self.assertNotIn("origin-contact", command)
        self.assertEqual(
            environment[handler.EXTEND_CONTACT_ENV],
            "origin-contact",
        )
        self.assertEqual(
            environment[handler.EXTEND_ACTING_CARBON_ENV],
            "origin-contact",
        )

    def test_resumed_codex_worker_inherits_contact_outside_command_line(self):
        process = self.FakeProcess()
        with tempfile.TemporaryDirectory() as temp:
            output_path = str(Path(temp) / "codex-output.jsonl")
            prompt_path = str(Path(temp) / "codex-prompt.md")
            with (
                mock.patch.object(
                    handler,
                    "_get_worker_record",
                    return_value={
                        "provider": "codex",
                        "session_id": "codex-thread",
                    },
                ),
                mock.patch.object(
                    handler,
                    "_write_codex_worker_prompt_file",
                    return_value=(prompt_path, ""),
                ),
                mock.patch.object(
                    handler,
                    "_run_output_path",
                    return_value=output_path,
                ),
                mock.patch.object(
                    handler.subprocess,
                    "Popen",
                    return_value=process,
                ) as popen,
                mock.patch.object(handler, "_record_active_run"),
            ):
                ok, _result = handler._launch_codex_worker_process(
                    "worker-b",
                    "resume task",
                    "terminal",
                    "origin-contact",
                    resume=True,
                    session_id="codex-thread",
                )

        self.assertTrue(ok)
        command = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        self.assertNotIn("origin-contact", command)
        self.assertEqual(
            environment[handler.EXTEND_CONTACT_ENV],
            "origin-contact",
        )
        self.assertEqual(
            environment[handler.EXTEND_ACTING_CARBON_ENV],
            "origin-contact",
        )

    def test_claude_worker_authentication_failure_names_claude(self):
        raw = json.dumps({
            "type": "assistant",
            "error": "authentication_failed",
            "message": {
                "content": [{
                    "type": "text",
                    "text": "Not logged in · Please run /login",
                }],
            },
        })

        self.assertEqual(
            handler._parse_claude_output(raw),
            "Claude not authenticated.",
        )

    def test_codex_worker_authentication_failure_names_codex(self):
        raw = json.dumps({
            "method": "error",
            "params": {
                "error": {
                    "message": "OAuth session expired",
                },
            },
        })

        self.assertEqual(
            handler._parse_codex_output(raw),
            "Codex not authenticated.",
        )


if __name__ == "__main__":
    unittest.main()
