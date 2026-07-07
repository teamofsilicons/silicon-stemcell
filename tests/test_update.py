import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

import update


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SystemUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_paths = {
            "DOTENV_FILE": update.DOTENV_FILE,
            "ENV_PY_FILE": update.ENV_PY_FILE,
            "GLASS_CONFIG_FILE": update.GLASS_CONFIG_FILE,
            "SILICON_CONFIG_FILE": update.SILICON_CONFIG_FILE,
            "SILICON_INFO_FILE": update.SILICON_INFO_FILE,
            "UPDATE_STATE_FILE": update.UPDATE_STATE_FILE,
        }
        update.DOTENV_FILE = self.root / ".env"
        update.ENV_PY_FILE = self.root / "env.py"
        update.GLASS_CONFIG_FILE = self.root / ".glass.json"
        update.SILICON_CONFIG_FILE = self.root / "silicon.json"
        update.SILICON_INFO_FILE = self.root / "silicon.info"
        update.UPDATE_STATE_FILE = self.root / "state" / "system_update.json"

    def tearDown(self):
        for key, value in self.old_paths.items():
            setattr(update, key, value)
        self.tmp.cleanup()

    def test_update_mismatch_spawns_brain_once_per_version(self):
        update.SILICON_INFO_FILE.write_text('{"version": "1.0"}\n', encoding="utf-8")
        latest = {
            "version_id": "1.1",
            "description": "new tools",
            "codebase_url": "https://glass.example/codebase.zip",
        }

        with mock.patch.object(update, "_fetch_latest_version", return_value=latest), mock.patch.object(
            update, "_spawn_update_brain", return_value=12345
        ) as spawn, mock.patch.object(update, "_apply_in_progress", return_value=False):
            first = update.check_for_system_update(now=4000)
            second = update.check_for_system_update(now=8000)

        # The update never rides a contact session — it spawns its own brain.
        self.assertEqual(first, {})
        self.assertEqual(second, {})
        spawn.assert_called_once()
        state = update._read_json(update.UPDATE_STATE_FILE, {})
        self.assertEqual(state["last_triggered_version"], "1.1")
        self.assertEqual(state["apply_pid"], 12345)

    def test_update_brain_message_is_diff_based(self):
        msg = update._update_message(
            {"description": "new tools", "codebase_url": "https://glass.example/codebase.zip"},
            "1.1",
        )
        self.assertIn("simple diff", msg)
        self.assertIn("new tools", msg)
        self.assertIn("https://glass.example/codebase.zip", msg)
        self.assertIn("exactly 1.1", msg)

    def test_up_to_date_does_not_spawn(self):
        update.SILICON_INFO_FILE.write_text('{"version": "1.1"}\n', encoding="utf-8")
        latest = {"version_id": "1.1", "description": "same", "codebase_url": "x"}
        with mock.patch.object(update, "_fetch_latest_version", return_value=latest), mock.patch.object(
            update, "_spawn_update_brain"
        ) as spawn:
            update.check_for_system_update(now=4000)
        spawn.assert_not_called()

    def test_fetch_latest_requests_and_stores_auth_key_when_missing(self):
        update.DOTENV_FILE.write_text("GLASS_SERVER_URL=https://glass.example\n", encoding="utf-8")
        update.ENV_PY_FILE.write_text('GLASS_API_KEY = ""\n', encoding="utf-8")
        update.SILICON_CONFIG_FILE.write_text('{"silicon_id": "si-1"}\n', encoding="utf-8")

        post_response = FakeResponse(201, {"auth_key": "scs_live_new"})
        get_response = FakeResponse(200, {"version_id": "1.1", "codebase_url": "https://code.zip"})
        with mock.patch.object(update.requests, "post", return_value=post_response) as post, mock.patch.object(
            update.requests, "get", return_value=get_response
        ) as get:
            latest = update._fetch_latest_version()

        self.assertEqual(latest["version_id"], "1.1")
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"]["silicon_id"], "si-1")
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Silicon-Key": "scs_live_new"})
        self.assertIn("SILICON_UPDATE_AUTH_KEY=scs_live_new", update.DOTENV_FILE.read_text(encoding="utf-8"))
        self.assertIn('GLASS_API_KEY = "scs_live_new"', update.ENV_PY_FILE.read_text(encoding="utf-8"))


class GlassAgentUpdateCommandTest(unittest.TestCase):
    """The agent's update command applies the Git updater and restarts if needed."""

    def _run(self, stdout):
        import glass_agent

        calls = []
        command = {"command": "update"}

        def fake_run(cmd, **kwargs):
            calls.append(("run", cmd, kwargs))
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        def fake_popen(cmd, **kwargs):
            calls.append(("popen", cmd, kwargs))
            return mock.Mock()

        with mock.patch.object(glass_agent.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(glass_agent.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(glass_agent.time, "sleep"):
            status, detail = glass_agent.execute_command(
                command, Path("/tmp/x"), "worker"
            )
        return status, detail, calls, command

    def test_git_update_result_restarts_silicon(self):
        status, detail, calls, command = self._run('{"status": "updated", "version": "1.5", "mode": "merge"}\n')

        self.assertEqual(status, "done")
        self.assertIn("updated to 1.5", detail)
        self.assertIn("restarting", detail)
        self.assertTrue(command["_agent_reexec"])
        run_cmds = [c[1] for c in calls if c[0] == "run"]
        self.assertEqual(run_cmds[0][0:2], [sys.executable, "-c"])
        self.assertIn("git_apply", run_cmds[0][2])
        self.assertEqual(calls[0][2]["cwd"], "/tmp/x")
        popen_cmds = [c[1] for c in calls if c[0] == "popen"]
        self.assertEqual(popen_cmds[0], ["sh", "-c", 'sleep 3; silicon restart "$1"', "_", "worker"])

    def test_up_to_date_result_does_not_restart(self):
        status, detail, calls, command = self._run('{"status": "up_to_date", "version": "1.5"}\n')
        self.assertEqual(status, "done")
        self.assertEqual(detail, "already on 1.5")
        self.assertNotIn("_agent_reexec", command)
        run_cmds = [c[1] for c in calls if c[0] == "run"]
        self.assertEqual(len(run_cmds), 1)
        self.assertEqual([c for c in calls if c[0] == "popen"], [])


if __name__ == "__main__":
    unittest.main()
