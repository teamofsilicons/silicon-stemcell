"""One agent, one file, forever — including across restarts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from diagnostics import logs


class AgentLogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = mock.patch.object(logs, "LOGS_DIR", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_each_kind_of_agent_gets_its_own_file(self):
        self.assertEqual(
            logs.AgentLog("manager", "carbon-a").path, self.root / "manager" / "carbon-a.log"
        )
        self.assertEqual(
            logs.AgentLog("advisor", "carbon-a").path, self.root / "advisor" / "carbon-a.log"
        )
        self.assertEqual(
            logs.AgentLog("worker", "browser-1").path, self.root / "worker" / "browser-1.log"
        )
        self.assertEqual(logs.AgentLog("silicon").path, self.root / "silicon.log")

    def test_an_advisor_never_writes_into_its_managers_file(self):
        logs.AgentLog("manager", "carbon-a").event("TOOL", "reply")
        logs.AgentLog("advisor", "carbon-a").event("ADVICE", "delegate it")

        manager_text = (self.root / "manager" / "carbon-a.log").read_text(encoding="utf-8")
        advisor_text = (self.root / "advisor" / "carbon-a.log").read_text(encoding="utf-8")
        self.assertIn("reply", manager_text)
        self.assertNotIn("delegate it", manager_text)
        self.assertIn("delegate it", advisor_text)

    def test_a_restart_appends_a_session_line_rather_than_starting_a_file(self):
        log = logs.AgentLog("manager", "carbon-a")
        log.session_start("session-1", "claude", "3.0.9")
        log.event("TOOL", "reply")
        log.session_start("session-2", "codex", "3.1.0")

        lines = (self.root / "manager" / "carbon-a.log").read_text(
            encoding="utf-8"
        ).strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("session_id=session-1", lines[0])
        self.assertIn("provider=claude", lines[0])
        self.assertIn("session_id=session-2", lines[2])
        self.assertIn("provider=codex", lines[2])

    def test_an_inference_call_records_what_went_in_and_what_came_out(self):
        log = logs.AgentLog("manager", "carbon-a")
        log.inference("in", provider="claude", input="do the thing")
        log.inference("out", provider="claude", output="done", seconds=1.5)

        records = [
            json.loads(line)
            for line in (self.root / "inference" / "manager-carbon-a.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        ]
        self.assertEqual([r["direction"] for r in records], ["in", "out"])
        self.assertEqual(records[0]["input"], "do the thing")
        self.assertEqual(records[1]["output"], "done")

    def test_a_contact_id_that_is_not_a_filename_still_gets_a_file(self):
        logs.AgentLog("manager", "carbon/../etc/passwd").event("TOOL", "reply")

        written = list((self.root / "manager").glob("*.log"))
        self.assertEqual(len(written), 1)
        self.assertNotIn("/", written[0].name.replace(".log", ""))

    def test_a_broken_log_never_raises_into_the_caller(self):
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            logs.AgentLog("manager", "carbon-a").event("TOOL", "reply")

    def test_the_same_agent_shares_one_log_object(self):
        self.assertIs(logs.agent_log("manager", "carbon-a"), logs.agent_log("manager", "carbon-a"))
        self.assertIsNot(logs.agent_log("manager", "carbon-a"), logs.agent_log("advisor", "carbon-a"))


if __name__ == "__main__":
    unittest.main()
