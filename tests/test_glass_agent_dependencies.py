import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glass_agent


class GlassAgentDependenciesTest(unittest.TestCase):
    def test_dependency_report_includes_pip_and_runtime_cli_versions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.txt").write_text("requests\nwebsockets>=12\n", encoding="utf-8")

            def latest_pypi(name):
                return {
                    "requests": ("2.0.0", ""),
                    "websockets": ("13.0", ""),
                    "silicon-cli": ("1.0.17", ""),
                    "silicon-browser": ("0.1.5", ""),
                }[name]

            def latest_npm(name):
                return {
                    "@anthropic-ai/claude-code": ("1.0.0", ""),
                    "@openai/codex": ("2.0.0", ""),
                    "@teamofsilicons/silicon-interface-cli": ("4.0.0", ""),
                }[name]

            with mock.patch.object(
                glass_agent, "_installed_python_version", side_effect=lambda name: {"requests": "1.0.0"}.get(name, "")
            ), mock.patch.object(
                glass_agent, "_latest_pypi_version", side_effect=latest_pypi
            ), mock.patch.object(
                glass_agent, "_npm_global_versions", return_value=({"@anthropic-ai/claude-code": "0.9.0", "@openai/codex": "2.0.0"}, "")
            ), mock.patch.object(
                glass_agent, "_version_from_command", return_value=""
            ), mock.patch.object(
                glass_agent, "_latest_npm_version", side_effect=latest_npm
            ), mock.patch.object(
                glass_agent, "_resolve_command", return_value=""
            ), mock.patch.object(
                glass_agent, "_command_identity", return_value=""
            ), mock.patch.object(
                glass_agent,
                "_python_console_package_version",
                side_effect=lambda root, command, package: {
                    "silicon-cli": "1.0.17",
                    "silicon-browser": "0.1.2",
                }.get(package, ""),
            ):
                report = glass_agent.dependency_report(root)

        by_name = {p["name"]: p for p in report["packages"]}
        self.assertEqual(by_name["requests"]["status"], "outdated")
        self.assertEqual(by_name["websockets"]["status"], "missing")
        self.assertEqual(by_name["@anthropic-ai/claude-code"]["status"], "outdated")
        self.assertEqual(by_name["@openai/codex"]["status"], "current")
        self.assertEqual(by_name["silicon-browser"]["manager"], "script")
        self.assertEqual(by_name["silicon-browser"]["package"], "silicon-browser")
        self.assertEqual(by_name["silicon-browser"]["status"], "outdated")
        self.assertEqual(by_name["silicon-interface"]["package"], "@teamofsilicons/silicon-interface-cli")
        self.assertEqual(by_name["silicon"]["manager"], "script")
        self.assertNotIn("glass", by_name)
        self.assertEqual(report["summary"]["outdated"], 3)
        self.assertEqual(report["summary"]["missing"], 2)

    def test_python_cli_update_uses_cli_owner_interpreter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_python = root / "python"
            cli = root / "silicon-browser"
            cli.write_text(f"#!{fake_python}\n", encoding="utf-8")

            with mock.patch.object(glass_agent, "_resolve_command", return_value=str(cli)), mock.patch.object(
                glass_agent, "_run_install", return_value={"ok": True, "returncode": 0, "detail": ""}
            ) as run_install:
                result = glass_agent._update_python_cli(
                    root,
                    {"command": "silicon-browser", "package": "silicon-browser"},
                )

        self.assertTrue(result["ok"])
        run_install.assert_called_once_with(
            [str(fake_python), "-m", "pip", "install", "--upgrade", "silicon-browser"],
            root,
            timeout=1200,
        )


if __name__ == "__main__":
    unittest.main()
