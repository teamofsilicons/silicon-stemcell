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


if __name__ == "__main__":
    unittest.main()
