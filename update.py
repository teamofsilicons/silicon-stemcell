"""Entry point for the self-updater.

Kept at the repository root because `python3 update.py rotate-key` is a
documented operator command; the implementation lives in ``interface/release/``.
"""
import sys

from interface.release.updater import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
