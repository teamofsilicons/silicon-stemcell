"""Every `iwantto` command group.

Each module registers its own arguments through ``add_parser(subparsers,
parser_cls)`` and attaches its handler with ``set_defaults(_handler=...)``, so
a command's flags and its behaviour stay in one file.
"""

from iwantto.commands import (
    advice,
    aux,
    delegate,
    messaging,
    remind,
    trust,
    work,
)

# Ordered as the CLI reference presents them.
COMMAND_MODULES = (
    messaging,
    work,
    trust,
    remind,
    advice,
    delegate,
    aux,
)

__all__ = ["COMMAND_MODULES"]
