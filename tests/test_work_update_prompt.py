import json
import re
import unittest
from pathlib import Path
from unittest import mock

from prompts import DNA


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORK_UPDATES_PATH = PROJECT_ROOT / "prompts" / "WORK_UPDATES.md"


def _json_examples(text):
    return re.findall(r"```json\n(.*?)\n```", text, flags=re.S)


def _one_line(text):
    return " ".join(text.split())


class WorkUpdatePromptTest(unittest.TestCase):
    def test_manager_prompt_includes_work_updates_once(self):
        with (
            mock.patch.object(DNA, "_get_contact_info", return_value=None),
            mock.patch.object(DNA, "_glass_profile_section", return_value=""),
            mock.patch.object(DNA, "_glass_team_context_section", return_value=""),
            mock.patch("core.extend.render_manager_catalog", return_value=""),
        ):
            manager_prompt = DNA.get_manager_prompt("carbon-1")

        self.assertEqual(manager_prompt.count("prompts/WORK_UPDATES.md"), 1)
        self.assertIn("# Work updates", manager_prompt)
        self.assertFalse(hasattr(DNA, "get_update_prompt"))
        self.assertFalse((PROJECT_ROOT / "prompts" / "UPDATE.md").exists())

    def test_manager_and_writer_resolve_one_shared_writing_guide(self):
        with (
            mock.patch.object(DNA, "_get_contact_info", return_value=None),
            mock.patch.object(DNA, "_glass_profile_section", return_value=""),
            mock.patch.object(DNA, "_glass_team_context_section", return_value=""),
            mock.patch("core.extend.render_manager_catalog", return_value=""),
        ):
            manager_prompt = DNA.get_manager_prompt("carbon-1")
        writer_prompt, error = DNA.get_worker_prompt("writer")

        self.assertEqual(error, "")
        self.assertEqual(manager_prompt.count("# Shared writing style"), 1)
        self.assertEqual(writer_prompt.count("# Shared writing style"), 1)
        self.assertNotIn(
            "{load-ref!prompts/shared/WRITING_STYLE.md}",
            manager_prompt,
        )
        self.assertNotIn(
            "{load-ref!prompts/shared/WRITING_STYLE.md}",
            writer_prompt,
        )
        self.assertNotIn("/productivity:start", manager_prompt)

    def test_every_work_update_json_example_is_valid_and_uses_documented_actions(self):
        text = WORK_UPDATES_PATH.read_text(encoding="utf-8")
        examples = _json_examples(text)
        self.assertGreater(len(examples), 0)

        actions = set()
        for index, example in enumerate(examples, 1):
            with self.subTest(example=index):
                parsed = json.loads(example)
                tools = parsed.get("tools")
                self.assertIsInstance(tools, list)
                self.assertGreater(len(tools), 0)
                for tool in tools:
                    self.assertEqual(tool.get("tool"), "work_update")
                    self.assertIsInstance(tool.get("action"), str)
                    actions.add(tool["action"])

        self.assertEqual(
            actions,
            {
                "task/create",
                "task/update",
                "todo/add",
                "todo/update",
                "milestone",
                "blocker/create",
                "blocker/resolve",
                "worker-group/create",
                "worker-group/update",
                "worker/create",
                "worker/update",
                "call/create",
                "call/update",
                "task/complete",
                "task/fail",
                "task/cancel",
            },
        )

    def test_prompt_encodes_task_timing_and_history_invariants(self):
        text = WORK_UPDATES_PATH.read_text(encoding="utf-8")
        prose = _one_line(text)

        for state in (
            "yet_to_start",
            "in_progress",
            "completed",
            "blocked",
        ):
            self.assertIn(f"`{state}`", text)
        self.assertIn("Call the task items **Todos**, not checklists", prose)
        self.assertIn("History is append-only", text)
        self.assertIn("ceil(realistic_estimate_seconds * 1.05)", text)
        self.assertIn("Queued work keeps its timer moving", text)
        self.assertIn(
            "Waiting for another Silicon also keeps its timer moving",
            prose,
        )
        for pause_reason in (
            "blocker",
            "rate_limited",
            "offline",
            "infrastructure",
        ):
            self.assertIn(f"`{pause_reason}`", text)
        self.assertIn("A terminal task must have a stopped timer", prose)
        self.assertIn("Elapsed time alone must never create a task or Todo", prose)
        self.assertIn(
            "runtime automatically schedules internal accuracy checkpoints "
            "at every 5% of the accepted goal time",
            prose,
        )
        self.assertIn("until the task reaches a terminal state", prose)
        self.assertIn(
            "rescheduled when the accepted estimate materially changes",
            prose,
        )
        self.assertIn("not `cron/create`", text)

    def test_prompt_encodes_truthful_blocker_worker_call_and_terminal_rules(self):
        text = WORK_UPDATES_PATH.read_text(encoding="utf-8")
        prose = _one_line(text)

        self.assertIn("Never fabricate motion", text)
        self.assertIn("does not resolve the blocker automatically", prose)
        self.assertIn("several open blockers", text)
        self.assertIn("Resolving one blocker must not imply", prose)
        self.assertIn("queued but not launched: `yet_to_start`", text)
        self.assertIn("provider or execution error: `failed`", text)
        self.assertIn("update-card failure must never cancel", text)
        self.assertIn("actual conversation in order", text)
        self.assertIn('Never replace the content with a count such as "3 messages"', prose)
        self.assertIn(
            "completes a call after 10 seconds without correlated manager or "
            "call activity",
            prose,
        )
        self.assertIn("Activity after that boundary is a new call", prose)
        self.assertIn("Use exactly one terminal action", text)
        self.assertIn(
            "After the terminal card is accepted, send a normal concise reply",
            prose,
        )

    def test_prompt_allows_normal_messages_and_requires_stable_retry_identity(self):
        text = WORK_UPDATES_PATH.read_text(encoding="utf-8")
        prose = _one_line(text)

        self.assertIn("Normal messages may appear between work updates", prose)
        self.assertIn("Keep every `task_id`", text)
        self.assertIn("Reuse the same identifiers for an exact retry", prose)
        self.assertIn("Never guess a revision", text)
        self.assertIn("do not claim it was published", text)
        self.assertIn("standalone call block", prose)
        self.assertIn("updated with its `call_id` and no `task_id`", prose)


if __name__ == "__main__":
    unittest.main()
