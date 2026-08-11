"""What the operator sees in the terminal while a manager works.

The Carbon-visible progress card says "executing command" on purpose: a shell
command can carry paths, internals, and secrets a Carbon has no business
seeing, so `progress_event` blanks commands and output at construction.

The person running Silicon needs the opposite. These renderers read the *raw*
provider event, which never leaves the process, so the log shows the exact
command and exactly what it printed — while the two hard boundaries (peer
advertising memory, private manager tool payloads) still hold.
"""
import os
import unittest
from unittest import mock

from interface.progress import (
    EXECUTING,
    progress_display_line,
    progress_event,
)
from inference.claude.progress import (
    claude_log_lines,
)
from inference.codex.progress import (
    codex_log_lines,
)


def _tool_use(name, tool_input, call_id="t1"):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}
            ]
        },
    }


def _tool_result(content, call_id="t1", is_error=False):
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                    "is_error": is_error,
                }
            ]
        },
    }


class CarbonSurfaceUnchangedTest(unittest.TestCase):
    def test_the_carbon_still_never_sees_a_command(self):
        event = progress_event(
            "claude", EXECUTING, status="started",
            tool_name="Bash", command="cat /etc/shadow",
        )

        self.assertEqual(event["command"], "[command omitted]")
        self.assertEqual(progress_display_line(event), "executing command")


class ClaudeLogTest(unittest.TestCase):
    def _run(self, *events):
        state, lines = {}, []
        for event in events:
            lines.extend(claude_log_lines(event, state))
        return lines

    def test_the_exact_command_and_its_output_are_shown(self):
        lines = self._run(
            _tool_use("Bash", {"command": "git status --short",
                               "description": "Check the tree"}),
            _tool_result(" M main.py\n?? core/iwantto/"),
        )

        self.assertIn("Bash: git status --short", lines)
        self.assertIn("    (Check the tree)", lines)
        self.assertIn("Bash done: git status --short", lines)
        self.assertIn("    │  M main.py", lines)
        self.assertIn("    │ ?? core/iwantto/", lines)

    def test_a_failure_is_marked_and_still_shows_its_output(self):
        lines = self._run(
            _tool_use("Bash", {"command": "pytest -q"}),
            _tool_result("1 failed, 744 passed", is_error=True),
        )

        self.assertIn("Bash FAILED: pytest -q", lines)
        self.assertIn("    │ 1 failed, 744 passed", lines)

    def test_file_and_search_calls_show_their_target(self):
        lines = self._run(
            _tool_use("Read", {"file_path": "/data/prompts/MEMORY.md"}, "r1"),
            _tool_use("WebSearch", {"query": "silicon stemcell"}, "s1"),
        )

        self.assertIn("Read: /data/prompts/MEMORY.md", lines)
        self.assertIn("WebSearch: silicon stemcell", lines)

    def test_claude_content_blocks_are_flattened(self):
        lines = self._run(
            _tool_use("Bash", {"command": "echo hi"}),
            _tool_result([{"type": "text", "text": "hi"}]),
        )

        self.assertIn("    │ hi", lines)

    def test_output_is_bounded_by_default_and_can_be_unbounded(self):
        with mock.patch.dict(os.environ, {"SILICON_LOG_OUTPUT_CHARS": "100"}):
            bounded = self._run(
                _tool_use("Bash", {"command": "cat big.log"}),
                _tool_result("x" * 5000),
            )
        with mock.patch.dict(os.environ, {"SILICON_LOG_OUTPUT_CHARS": "0"}):
            unbounded = self._run(
                _tool_use("Bash", {"command": "cat big.log"}),
                _tool_result("y" * 5000),
            )

        self.assertTrue(any("more characters" in line for line in bounded))
        self.assertLess(sum(len(line) for line in bounded), 400)
        self.assertGreater(sum(len(line) for line in unbounded), 5000)

    def test_credentials_are_redacted_from_commands_and_output(self):
        lines = self._run(
            _tool_use("Bash", {
                "command": "curl -H 'Authorization: Bearer sk_live_SUPERSECRET1234'"
            }),
            _tool_result("SILICON_API_KEY=scs_live_abcdef123456\nok"),
        )
        rendered = "\n".join(lines)

        self.assertNotIn("SUPERSECRET1234", rendered)
        self.assertNotIn("scs_live_abcdef123456", rendered)
        self.assertIn("[redacted", rendered)

    def test_advertising_output_is_suppressed_by_call_id_not_by_content(self):
        """The result of reading an advertising file contains no path to match.

        It has to be suppressed because the *call* was for advertising memory,
        so the correlation from tool_use to tool_result is what protects it.
        """
        for tool, tool_input in (
            ("Bash", {"command": "cat prompts/advertising/peer-1.md"}),
            ("Read", {"file_path": "prompts/advertising/peer-1.md"}),
        ):
            with self.subTest(tool=tool):
                lines = self._run(
                    _tool_use(tool, tool_input, "a1"),
                    _tool_result("PEER ADVERTISING CONTENT", "a1"),
                )
                rendered = "\n".join(lines)

                self.assertNotIn("PEER ADVERTISING CONTENT", rendered)
                self.assertIn("[advertising memory content omitted]", rendered)

    def test_private_manager_payloads_stay_redacted(self):
        lines = self._run(
            _tool_result(
                '{"tools":[{"tool":"advertising_memory/update","content":"PRIVATE"}]}',
                "x1",
            )
        )

        self.assertNotIn("PRIVATE", "\n".join(lines))


class CodexLogTest(unittest.TestCase):
    @staticmethod
    def _item(item_id, command, output=None, exit_code=None):
        item = {"id": item_id, "type": "commandExecution", "command": command}
        if output is not None:
            item["aggregatedOutput"] = output
        if exit_code is not None:
            item["exitCode"] = exit_code
        return item

    def _run(self, *messages):
        state, lines = {}, []
        for message in messages:
            lines.extend(codex_log_lines(message, state))
        return lines

    def test_the_exact_command_and_output_are_shown(self):
        lines = self._run(
            {"method": "item/started",
             "params": {"item": self._item("c1", ["git", "status"])}},
            {"method": "item/completed",
             "params": {"item": self._item(
                 "c1", ["git", "status"], "On branch main", 0)}},
        )

        self.assertIn("commandExecution: git status", lines)
        self.assertIn("commandExecution done exit=0: git status", lines)
        self.assertIn("    │ On branch main", lines)

    def test_a_nonzero_exit_is_marked_failed(self):
        lines = self._run(
            {"method": "item/started",
             "params": {"item": self._item("c2", ["false"])}},
            {"method": "item/completed",
             "params": {"item": self._item("c2", ["false"], "", 1)}},
        )

        self.assertTrue(any("FAILED exit=1" in line for line in lines))

    def test_advertising_output_is_suppressed(self):
        lines = self._run(
            {"method": "item/started",
             "params": {"item": self._item(
                 "c3", ["cat", "prompts/advertising/peer-1.md"])}},
            {"method": "item/completed",
             "params": {"item": self._item(
                 "c3", ["cat", "prompts/advertising/peer-1.md"],
                 "PEER ADVERTISING CONTENT", 0)}},
        )
        rendered = "\n".join(lines)

        self.assertNotIn("PEER ADVERTISING CONTENT", rendered)
        self.assertIn("[advertising memory content omitted]", rendered)


if __name__ == "__main__":
    unittest.main()
