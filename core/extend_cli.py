"""One-release import compatibility for the Silicon Extend package CLI.

New code and workers should invoke the installed ``silicon-extend`` entry point
directly. The Stemcell no longer creates a data-root launcher.
"""
from __future__ import annotations

import sys
from collections.abc import Sequence


def run(argv: Sequence[str] | None = None) -> int:
    try:
        from silicon_extend.cli import main as package_main
    except ImportError:
        print(
            "silicon-extend is not installed in the active Silicon environment.",
            file=sys.stderr,
        )
        return 3
    try:
        result = package_main(None if argv is None else list(argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    return int(result or 0)


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
