import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from core import extend_cli


class SiliconExtendCliCompatibilityTest(unittest.TestCase):
    def test_wrapper_delegates_to_package_cli(self):
        cli_module = types.ModuleType("silicon_extend.cli")
        cli_module.main = mock.Mock(return_value=7)

        with mock.patch.dict(
            sys.modules,
            {"silicon_extend.cli": cli_module},
        ):
            result = extend_cli.run(["status", "--json"])

        self.assertEqual(result, 7)
        cli_module.main.assert_called_once_with(["status", "--json"])

    def test_wrapper_preserves_package_system_exit_code(self):
        cli_module = types.ModuleType("silicon_extend.cli")
        cli_module.main = mock.Mock(side_effect=SystemExit(2))

        with mock.patch.dict(
            sys.modules,
            {"silicon_extend.cli": cli_module},
        ):
            result = extend_cli.run(["unknown"])

        self.assertEqual(result, 2)

    def test_stemcell_does_not_define_or_generate_an_extend_launcher(self):
        self.assertFalse(hasattr(extend_cli, "ensure_launcher"))
        source = Path(extend_cli.__file__).read_text(encoding="utf-8")
        self.assertNotIn(".local/bin", source)
        self.assertNotIn("os.replace", source)


if __name__ == "__main__":
    unittest.main()
