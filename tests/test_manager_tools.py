import json
import re
import unittest
from pathlib import Path
from unittest import mock

import main


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
                    "executing",
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


if __name__ == "__main__":
    unittest.main()
