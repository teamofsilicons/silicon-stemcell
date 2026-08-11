import unittest

from interface.progress import (
    EXECUTING,
    progress_display_line,
    progress_event,
)
from interface.redaction import (
    redact_private_manager_output,
)
from inference.claude.progress import (
    claude_progress_events,
)
from inference.codex.progress import (
    codex_progress_event,
)


class ProgressDisplayTest(unittest.TestCase):
    def test_executing_command_does_not_expose_command_or_output_while_running_or_successful(self):
        for status in ("started", "output", "completed"):
            with self.subTest(status=status):
                line = progress_display_line(
                    progress_event(
                        "codex",
                        EXECUTING,
                        status=status,
                        command="python manage.py migrate --database prod",
                        preview="/Users/codanium/Documents/silicon/private/path.py",
                        exit_code=0,
                    )
                )
                self.assertEqual(line, "executing command")

    def test_failed_executing_command_omits_durable_output_and_command(self):
        line = progress_display_line(
            progress_event(
                "codex",
                EXECUTING,
                status="completed",
                command="python manage.py migrate --database prod",
                preview="Traceback: target id not found",
                exit_code=1,
            )
        )

        self.assertEqual(
            line,
            "executing command failed: [command output omitted]",
        )
        self.assertNotIn("manage.py", line)
        self.assertNotIn("Traceback", line)

    def test_advertising_memory_payload_is_redacted(self):
        content = "team-visible but not process-log content"
        output = (
            '{"tools":[{"tool":"advertising_memory/update",'
            f'"content":"{content}"}}]}}'
        )

        rendered = redact_private_manager_output(output)

        self.assertEqual(rendered, "[private manager tool invocation omitted]")
        self.assertNotIn(content, rendered)

    def test_escaped_private_tool_names_are_redacted(self):
        content = "do not persist this payload"
        values = [
            (
                '{"tools":[{"content":"do not persist this payload",'
                '"tool":"advertising_memory\\u002fupdate"}]}'
            ),
        ]

        for value in values:
            with self.subTest(value=value):
                rendered = redact_private_manager_output(value)
                self.assertEqual(
                    rendered,
                    "[private manager tool invocation omitted]",
                )
                self.assertNotIn(content, rendered)

    def test_incomplete_escaped_private_tool_invocation_is_redacted(self):
        content = "SECRET IN A TRUNCATED TOOL CALL"
        values = (
            (
                '{"tools":[{"tool":"advertising_memory\\u002fupdate",'
                f'"content":"{content}"'
            ),
            (
                '{"tools":[{"tool":"advertising_memory\\/update",'
                f'"content":"{content}"'
            ),
        )

        for value in values:
            with self.subTest(value=value):
                rendered = redact_private_manager_output(value)
                self.assertEqual(
                    rendered,
                    "[private manager tool invocation omitted]",
                )
                self.assertNotIn(content, rendered)

    def test_advertising_file_tool_result_is_removed_from_progress_event(self):
        state = {}
        started = claude_progress_events(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "read-ad",
                            "name": "Read",
                            "input": {
                                "file_path": "/instance/prompts/advertising/peer.md"
                            },
                        }
                    ]
                },
            },
            state,
        )
        self.assertEqual(len(started), 1)

        completed = claude_progress_events(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "read-ad",
                            "content": "PEER ADVERTISING CONTENT",
                        }
                    ]
                },
            },
            state,
        )[0]

        self.assertEqual(
            completed["output"],
            "[advertising memory content omitted]",
        )
        self.assertNotIn("PEER ADVERTISING CONTENT", repr(completed))

    def test_codex_advertising_command_delta_is_removed_from_progress_event(self):
        state = {}
        codex_progress_event(
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "read-ad",
                        "type": "commandExecution",
                        "command": "cat prompts/advertising/peer.md",
                    }
                },
            },
            state,
        )
        event = codex_progress_event(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "itemId": "read-ad",
                    "delta": "PEER ADVERTISING CONTENT",
                },
            },
            state,
        )

        self.assertEqual(event["delta"], "[command output omitted]")
        self.assertNotIn("PEER ADVERTISING CONTENT", repr(event))

    def test_shell_obfuscation_cannot_expose_command_or_output(self):
        state = {}
        started = codex_progress_event(
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "obfuscated-read",
                        "type": "commandExecution",
                        "command": (
                            "cat prompts/a[d]vertising/peer.md; "
                            "cat prompts/advert\\ising/peer.md"
                        ),
                    }
                },
            },
            state,
        )
        delta = codex_progress_event(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "itemId": "obfuscated-read",
                    "delta": "PEER ADVERTISING CONTENT",
                },
            },
            state,
        )

        self.assertEqual(started["command"], "[command omitted]")
        self.assertEqual(delta["delta"], "[command output omitted]")
        self.assertNotIn("advertising", repr(started).lower())
        self.assertNotIn("PEER ADVERTISING CONTENT", repr(delta))

    def test_codex_mcp_read_tracks_private_path_from_arguments(self):
        state = {}
        started = codex_progress_event(
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "mcp-read-ad",
                        "type": "mcpToolCall",
                        "server": "filesystem",
                        "tool": "read_file",
                        "arguments": {
                            "path": "prompts/advertising/peer.md",
                        },
                    }
                },
            },
            state,
        )
        completed = codex_progress_event(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "mcp-read-ad",
                        "type": "mcpToolCall",
                        "server": "filesystem",
                        "tool": "read_file",
                        "aggregatedOutput": "PEER ADVERTISING CONTENT",
                    }
                },
            },
            state,
        )

        self.assertEqual(started["kind"], "reading_file")
        self.assertEqual(
            completed["output"],
            "[advertising memory content omitted]",
        )
        self.assertNotIn("PEER ADVERTISING CONTENT", repr(completed))

    def test_advertising_aliases_and_private_draft_archive_are_redacted(self):
        references = (
            "cat prompts/advertising/*.md",
            "cat prompts/advertising/../advertising/peer.md",
            "cat prompts/./advertising/self.md",
            "cat prompts/x/../advertising/self.md",
            "cat core/interface_state/team_context_drafts/self/draft.md",
            "cat core/./interface_state/x/../team_context_drafts/self/draft.md",
        )
        for reference in references:
            with self.subTest(reference=reference):
                rendered = redact_private_manager_output(
                    f"{reference}\nSECRET ADVERTISING CONTENT"
                )
                self.assertEqual(
                    rendered,
                    "[advertising memory content omitted]",
                )

    def test_advertising_related_commands_and_queries_are_fully_redacted(self):
        secret = "ADVERTISING SECRET"
        event = progress_event(
            "codex",
            EXECUTING,
            command=(
                f"printf '{secret}' > "
                "/instance/prompts/./advertising/self.md"
            ),
            query=(
                f"{secret} "
                "core/./interface_state/team_context_drafts/self/draft.md"
            ),
            status="started",
        )

        self.assertEqual(
            event["command"],
            "[advertising memory content omitted]",
        )
        self.assertEqual(
            event["query"],
            "[advertising memory content omitted]",
        )
        self.assertNotIn(secret, repr(event))


if __name__ == "__main__":
    unittest.main()
