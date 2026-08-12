import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[2]


class RuntimeDataRootTests(unittest.TestCase):
    def test_generation_keeps_runtime_state_in_instance_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary).resolve()
            release_root = (
                data_root / ".silicon" / "releases" / "test-generation"
            )
            for directory in (
                "diagnostics",
                "helpers",
                "inference",
                "interface",
                "manager",
                "prompts",
                "worker",
            ):
                shutil.copytree(
                    CODE_ROOT / directory,
                    release_root / directory,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                )
            for filename in (
                ".silicon-data-root-v1",
                "glass_agent.py",
                "main.py",
                "silicon.info",
                "update.py",
            ):
                shutil.copy2(CODE_ROOT / filename, release_root / filename)
            (data_root / "prompts").mkdir()
            (data_root / "prompts" / "MEMORY.md").write_text(
                "instance memory\n",
                encoding="utf-8",
            )
            (data_root / "silicon.json").write_text(
                '{"brain":"claude"}\n',
                encoding="utf-8",
            )
            (data_root / ".backupsilicon").write_text("", encoding="utf-8")

            script = textwrap.dedent(
                """
                import json
                import os
                import sys
                import types
                from pathlib import Path

                # This test environment may not install runtime HTTP
                # dependencies. Path initialization does not use the network.
                requests = types.ModuleType("requests")
                class RequestException(Exception):
                    pass
                requests.RequestException = RequestException
                requests.get = lambda *args, **kwargs: None
                requests.post = lambda *args, **kwargs: None
                sys.modules.setdefault("requests", requests)

                from helpers.paths import CODE_ROOT, DATA_ROOT
                from diagnostics import activity as activity_log
                from diagnostics import store as diagnostics
                import interface
                from interface import backup, cron, messages
                from interface import team as team_context
                from interface import config as glass
                from interface import work as work_updates
                from interface.cron import checkback
                from manager.runtime.maintenance import MaintenanceCoordinator
                from prompts import loader as DNA
                from interface.agent import config as agent_config
                import inference
                from inference.codex import provider as codex_provider
                import main
                import manager
                import manager.settings as m_manager_settings
                from interface.release import updater as update
                import worker.constants
                from worker import pool as handler
                from worker import registry as registry_module

                data = DATA_ROOT
                messages._save_manager_messages({"carbon-a": [{"message": "hi"}]})
                checkback.add_checkback("worker-a", "carbon-a", 5)
                manager.new_session("carbon-a", brain="claude")
                registry_module._save_active({"worker-a": {"pid": 1}})
                activity_log.log("TEST", "runtime root")
                MaintenanceCoordinator().request_drain(
                    maintenance_id="update-test",
                    deadline_seconds=30,
                )
                snapshot = backup.create_local_snapshot(
                    data,
                    release_id="release-test",
                )

                result = {
                    "code_root": str(CODE_ROOT),
                    "data_root": str(DATA_ROOT),
                    "interface_state": str(interface.STATE_DIR),
                    "glass_root": str(glass.PROJECT_ROOT),
                    "team_root": str(team_context.PROJECT_ROOT),
                    "cron_state": str(cron.CRON_STATE_FILE),
                    "diagnostics": str(diagnostics.DEFAULT_DIAG_DIR),
                    "work_updates": str(work_updates.WORK_UPDATES_FILE),
                    "manager_sessions": str(Path(inference.SESSIONS_DIR)),
                    "manager_config": str(Path(inference.config.SILICON_CONFIG_FILE)),
                    "worker_outputs": str(Path(worker.constants.OUTPUTS_DIR)),
                    "worker_state": str(Path(worker.constants.WORKER_STATE_DIR)),
                    "worker_code": str(Path(codex_provider.APP_WORKER)),
                    "worker_workspace": str(Path(worker.constants.WORKSPACE_ROOT)),
                    "backup_default": str(backup._instance_root()),
                    "update_state": str(update.UPDATE_STATE_FILE),
                    "update_info": str(update.SILICON_INFO_FILE),
                    "restart_flag": str(m_manager_settings.RESTART_FLAG),
                    "glass_agent_root": str(agent_config.silicon_dir()),
                    "memory_prompt": DNA._read_prompt("MEMORY.md"),
                    "manager_prompt_path": DNA._prompt_path("MANAGER.md"),
                    "ownership_prompt": DNA._persistent_runtime_paths_section(),
                    "worker_ownership_prompt": DNA.get_worker_prompt("terminal")[0],
                    "snapshot_paths": [
                        item["path"] for item in snapshot.manifest["files"]
                    ],
                }
                print(json.dumps(result, sort_keys=True))
                """
            )
            environment = dict(os.environ)
            environment["SILICON_DATA_ROOT"] = str(data_root)
            environment["SILICON_RELEASE_ROOT"] = str(release_root)
            environment["PYTHONPATH"] = str(release_root)
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=release_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            result = json.loads(completed.stdout.strip().splitlines()[-1])

            self.assertEqual(Path(result["code_root"]), release_root)
            self.assertEqual(Path(result["data_root"]), data_root)
            self.assertEqual(
                (release_root / ".silicon-data-root-v1").read_text(
                    encoding="utf-8"
                ).strip(),
                "1",
            )
            for key in (
                "interface_state",
                "glass_root",
                "team_root",
                "cron_state",
                "diagnostics",
                "work_updates",
                "manager_sessions",
                "manager_config",
                "worker_outputs",
                "worker_state",
                "backup_default",
                "update_state",
                "restart_flag",
                "glass_agent_root",
            ):
                self.assertTrue(
                    Path(result[key]).is_relative_to(data_root),
                    (key, result[key]),
                )
            self.assertTrue(
                Path(result["worker_code"]).is_relative_to(release_root)
            )
            self.assertEqual(Path(result["worker_workspace"]), release_root)
            self.assertTrue(
                Path(result["update_info"]).is_relative_to(release_root)
            )
            self.assertIn("instance memory", result["memory_prompt"])
            self.assertIn(str(data_root), result["ownership_prompt"])
            self.assertIn(
                str(release_root),
                result["worker_ownership_prompt"],
            )
            # The manager's own prompts live next to the manager's code now.
            self.assertTrue(
                Path(result["manager_prompt_path"]).is_relative_to(
                    release_root / "manager" / "prompts"
                )
            )
            self.assertIn("prompts/MEMORY.md", result["snapshot_paths"])

            self.assertTrue(
                (
                    data_root
                    / "interface"
                    / "state"
                    / "manager_queue.json"
                ).is_file()
            )
            self.assertTrue(
                (data_root / "interface" / "cron" / "checkbacks.json").is_file()
            )
            self.assertTrue(
                (data_root / "interface" / "state" / "maintenance.json").is_file()
            )
            self.assertTrue((data_root / "sessions" / "carbon-a.txt").is_file())
            self.assertTrue(
                (
                    data_root
                    / "interface"
                    / "state"
                    / "workers"
                    / "_active_workers.json"
                ).is_file()
            )
            self.assertTrue((data_root / "logs" / "silicon.log").is_file())

    def test_rejects_relative_or_release_store_data_root(self):
        from helpers.paths import RuntimePathError, validated_data_root

        with self.assertRaisesRegex(RuntimePathError, "absolute"):
            validated_data_root("relative-instance")

        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / ".silicon" / "releases" / "generation"
            release.mkdir(parents=True)
            with self.assertRaisesRegex(RuntimePathError, "releases"):
                validated_data_root(release)


if __name__ == "__main__":
    unittest.main()
