import tempfile
import unittest
from pathlib import Path
from unittest import mock

from interface.agent import live as glass_agent


class GlassAgentDependenciesTest(unittest.TestCase):
    def test_dependency_report_includes_pip_and_runtime_cli_versions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.txt").write_text(
                "requests\nwebsockets>=12\n",
                encoding="utf-8",
            )

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

            with (
                mock.patch.object(
                    glass_agent,
                    "_installed_python_version",
                    side_effect=lambda name: {
                        "requests": "1.0.0",
                    }.get(name, ""),
                ),
                mock.patch.object(
                    glass_agent,
                    "_latest_pypi_version",
                    side_effect=latest_pypi,
                ),
                mock.patch.object(
                    glass_agent,
                    "_npm_global_versions",
                    return_value=(
                        {
                            "@anthropic-ai/claude-code": "0.9.0",
                            "@openai/codex": "2.0.0",
                        },
                        "",
                    ),
                ),
                mock.patch.object(
                    glass_agent,
                    "_version_from_command",
                    return_value="",
                ),
                mock.patch.object(
                    glass_agent,
                    "_latest_npm_version",
                    side_effect=latest_npm,
                ),
                mock.patch.object(
                    glass_agent,
                    "_resolve_command",
                    return_value="",
                ),
                mock.patch.object(
                    glass_agent,
                    "_command_identity",
                    return_value="",
                ),
                mock.patch.object(
                    glass_agent,
                    "_python_console_package_version",
                    side_effect=lambda root, command, package: {
                        "silicon-cli": "1.0.17",
                        "silicon-browser": "0.1.2",
                    }.get(package, ""),
                ),
            ):
                report = glass_agent.dependency_report(root)

        by_name = {package["name"]: package for package in report["packages"]}
        self.assertEqual(by_name["requests"]["status"], "outdated")
        self.assertEqual(by_name["websockets"]["status"], "missing")
        self.assertEqual(
            by_name["@anthropic-ai/claude-code"]["status"],
            "outdated",
        )
        self.assertEqual(by_name["@openai/codex"]["status"], "current")
        self.assertEqual(by_name["silicon-browser"]["manager"], "script")
        self.assertEqual(
            by_name["silicon-interface"]["package"],
            "@teamofsilicons/silicon-interface-cli",
        )
        self.assertEqual(by_name["silicon"]["manager"], "script")
        self.assertNotIn("glass", by_name)
        self.assertEqual(report["summary"]["outdated"], 3)
        self.assertEqual(report["summary"]["missing"], 2)

    def test_live_dependency_update_is_refused_without_installing(self):
        report = {
            "checked_at": "2026-07-23T00:00:00Z",
            "packages": [],
            "summary": {
                "total": 0,
                "current": 0,
                "outdated": 0,
                "missing": 0,
                "unknown": 0,
            },
            "errors": [],
        }
        command = {"command": "dependency_update"}
        with mock.patch.object(
            glass_agent,
            "dependency_report",
            return_value=report,
        ) as dependency_report:
            status, detail = glass_agent.execute_command(
                command,
                Path("/tmp/example"),
                "silicon",
            )

        self.assertEqual(status, "failed")
        self.assertIn("silicon update <name>", detail)
        self.assertNotIn("stop the team", detail.lower())
        self.assertEqual(command["_status_patch"]["dependencies"], report)
        dependency_report.assert_called_once_with(Path("/tmp/example"))


if __name__ == "__main__":
    unittest.main()
