"""`iwantto` — the command line a Silicon acts through.

Each command group registers its own arguments and handler (see
``core.iwantto.commands``), so the flags for a command live next to the code
that honours them.  The dispatcher's own job is small: identify the caller,
route to the handler, record what happened, and turn any failure into a
sentence the Silicon running it can act on.

Nothing here defers work. A command runs to completion before the process
exits, so a manager reading stdout is reading the finished result.
"""
from __future__ import annotations

import argparse
import sys

from core.iwantto import journal
from core.iwantto.actor import ActorError, resolve_actor
from core.iwantto.commands import COMMAND_MODULES

PROGRAM = "iwantto"


class CommandError(RuntimeError):
    """A command failed for a reason the caller should read and act on."""


class _Parser(argparse.ArgumentParser):
    """Argparse that reports usage problems as errors instead of exiting.

    A Silicon reads stdout and decides what to do next. Argparse's default
    "print usage, exit 2" is a dead end mid-run, so parse failures come back as
    an ordinary error string like any other command failure.
    """

    def error(self, message):
        raise CommandError(f"{self.prog}: {message}")

    def exit(self, status=0, message=None):
        if status:
            raise CommandError(message or f"{self.prog}: invalid arguments")
        # --help already printed; unwind without killing the interpreter.
        raise SystemExit(0)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROGRAM,
        description="Act as this Silicon: message, delegate, track work, "
        "manage trust, set reminders, and ask your advisor.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    for module in COMMAND_MODULES:
        module.add_parser(subparsers, _Parser)
    return parser


def _pending_mail(actor) -> str:
    """Anything left for this actor mid-run, appended to the command's output.

    This is how a running worker hears an answer without being interrupted: the
    next command it runs carries the reply back with its own result.
    """
    try:
        from core.iwantto import mailbox

        return mailbox.format_mail(mailbox.drain(actor.kind, actor.actor_id))
    except Exception:
        return ""


def _dispatch(args, actor) -> str:
    handler = getattr(args, "_handler", None)
    if handler is None:
        raise CommandError(
            "Tell me what you want to do. Run `iwantto --help` for the list."
        )
    return handler(args, actor)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    try:
        actor = resolve_actor()
    except ActorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    command = argv[0] if argv else ""
    try:
        args = parser.parse_args(argv)
        command = str(getattr(args, "command", "") or command)
        result = _dispatch(args, actor)
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    except CommandError as exc:
        message = str(exc)
        print(f"Error: {message}", file=sys.stderr)
        journal.record(actor, command, args=argv, result=message, ok=False)
        journal.note_invocation(actor.token, command, ok=False)
        return 1
    except Exception as exc:  # a handler failed in a way it did not anticipate
        message = f"{type(exc).__name__}: {exc}"
        print(f"Error: {message}", file=sys.stderr)
        journal.record(actor, command, args=argv, result=message, ok=False)
        journal.note_invocation(actor.token, command, ok=False)
        return 1

    text = str(result or "")
    trailer = _pending_mail(actor)
    if text or trailer:
        print((text + trailer).strip("\n"))
    journal.record(actor, command, args=argv, result=text, ok=True)
    journal.note_invocation(actor.token, command, ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
