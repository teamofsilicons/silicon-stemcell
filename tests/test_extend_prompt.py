import unittest
from pathlib import Path
from unittest import mock

from prompts import DNA

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUIDE_PATH = PROJECT_ROOT / "prompts" / "EXTEND_TOOLS.md"
MANAGER_TOOLS_PATH = PROJECT_ROOT / "prompts" / "MANAGER_TOOLS.md"
CATALOG = (
    "## Enabled Silicon Extend integrations\n"
    "<silicon-extend-catalog>\n"
    "- `integration/gmail` — Gmail\n"
    "  access: This Silicon has access to Gmail.\n"
    "</silicon-extend-catalog>"
)


class ExtendPromptAssemblyTest(unittest.TestCase):
    def _manager_prompt(self, *, catalog=CATALOG):
        with (
            mock.patch.object(DNA, "_get_contact_info", return_value=None),
            mock.patch.object(DNA, "_glass_profile_section", return_value=""),
            mock.patch.object(DNA, "_glass_team_context_section", return_value=""),
            mock.patch(
                "core.extend.render_manager_catalog",
                return_value=catalog,
            ),
        ):
            return DNA.get_manager_prompt("carbon-1")

    def test_manager_loads_durable_guide_after_private_tools_then_live_catalog(self):
        prompt = self._manager_prompt()

        manager_tools_at = prompt.index("prompts/MANAGER_TOOLS.md")
        extend_guide_at = prompt.index("prompts/EXTEND_TOOLS.md")
        catalog_at = prompt.index("## Enabled Silicon Extend integrations")

        self.assertLess(manager_tools_at, extend_guide_at)
        self.assertLess(extend_guide_at, catalog_at)
        self.assertEqual(prompt.count("prompts/EXTEND_TOOLS.md"), 1)
        self.assertEqual(prompt.count("## Enabled Silicon Extend integrations"), 1)

    def test_every_worker_loads_the_same_guide_and_exact_live_catalog(self):
        for worker_type in DNA.VALID_WORKER_TYPES:
            with self.subTest(worker_type=worker_type):
                with mock.patch(
                    "core.extend.render_manager_catalog",
                    return_value=CATALOG,
                ):
                    prompt, error = DNA.get_worker_prompt(worker_type)

                self.assertEqual(error, "")
                self.assertEqual(prompt.count("prompts/EXTEND_TOOLS.md"), 1)
                self.assertEqual(prompt.count(CATALOG), 1)
                self.assertLess(
                    prompt.index("prompts/EXTEND_TOOLS.md"),
                    prompt.index("## Enabled Silicon Extend integrations"),
                )

    def test_guide_remains_when_the_live_catalog_is_unavailable(self):
        with mock.patch(
            "core.extend.render_manager_catalog",
            side_effect=RuntimeError("Glass unavailable"),
        ):
            worker_prompt, error = DNA.get_worker_prompt("terminal")

        manager_prompt = self._manager_prompt(catalog="")

        self.assertEqual(error, "")
        self.assertIn("prompts/EXTEND_TOOLS.md", manager_prompt)
        self.assertIn("prompts/EXTEND_TOOLS.md", worker_prompt)
        self.assertNotIn("## Enabled Silicon Extend integrations", manager_prompt)
        self.assertNotIn("## Enabled Silicon Extend integrations", worker_prompt)

    def test_guide_keeps_manager_and_worker_invocation_contracts_distinct(self):
        text = GUIDE_PATH.read_text(encoding="utf-8")

        self.assertIn("installed Silicon Extend package", text)
        self.assertIn("shared team directory as authoritative", text)
        self.assertIn("Standalone installations keep", text)
        self.assertIn('"tool": "extend"', text)
        self.assertIn('"type": "request_setup"', text)
        self.assertIn("silicon-extend list --json", text)
        self.assertIn("silicon-extend ready --json", text)
        self.assertIn("silicon-extend needs-setup --json", text)
        self.assertIn("silicon-extend show gmail.messages.send --json", text)
        self.assertIn("silicon-extend run gmail.messages.send --json", text)
        self.assertIn("silicon-extend setup gmail.messages.send", text)
        self.assertIn("silicon-extend integration help gmail --json", text)
        self.assertIn("silicon-extend integration create --file", text)
        self.assertIn("silicon-extend integration import-openapi", text)
        self.assertIn("silicon-extend tool create internal --file", text)
        self.assertIn("written to the current team in Glass", text)
        self.assertNotIn("python -m worker.extend_cli", text)
        self.assertIn("Do not emit manager", text)
        self.assertIn("standard input", text)
        self.assertIn(
            "worker transcript may still be archived",
            " ".join(text.split()),
        )
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)

    def test_manager_tools_directly_documents_extend_discovery_and_usage(self):
        text = MANAGER_TOOLS_PATH.read_text(encoding="utf-8")

        self.assertIn("### Silicon Extend", text)
        self.assertIn('"type": "list"', text)
        self.assertIn('"type": "ready"', text)
        self.assertIn('"type": "needs_setup"', text)
        self.assertIn('"type": "pending"', text)
        self.assertIn('"type": "status"', text)
        self.assertIn('"type": "show"', text)
        self.assertIn('"type": "connections"', text)
        self.assertIn('"type": "requests"', text)
        self.assertIn('"type": "execute"', text)
        self.assertIn('"type": "request_setup"', text)
        self.assertIn("Fetch all tools enabled for this Silicon", text)
        self.assertIn('"type": "integrations"', text)
        self.assertIn('"tool": "integration/gmail"', text)
        self.assertIn("Silicon has access", text)
        self.assertIn("not expose every operation", text)
        self.assertIn("Glass manages the full system catalog", text)

    def test_guide_documents_inline_setup_without_credentials_or_glass_redirect(self):
        text = " ".join(GUIDE_PATH.read_text(encoding="utf-8").split())

        self.assertIn("durable chat message for the assigned Carbon", text)
        self.assertIn("inside Interface", text)
        self.assertIn("Do not send them to Glass", text)
        self.assertIn("do not ask them to paste a credential into chat", text)
        self.assertIn("never credentials, tokens, integration URLs", text)


if __name__ == "__main__":
    unittest.main()
