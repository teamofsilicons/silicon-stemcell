"""`iwantto get-advice` — ask your advisor, and wait for the answer.

This command blocks. Advice that arrives after the manager has already acted is
not advice, so the advisor runs to completion and its answer comes back on
stdout before the manager's next command.
"""
from __future__ import annotations


def _error(message):
    from core.iwantto.cli import CommandError

    return CommandError(message)


def cmd_get_advice(args, actor) -> str:
    from core import advisor

    if actor.is_advisor:
        raise _error(
            "You are the advisor. You give advice; you do not ask for it."
        )
    if not actor.is_manager:
        raise _error(
            "Only a manager can ask for advice. If you are a worker and you "
            'need something, use `iwantto send manager --text "..."`.'
        )
    question = str(args.question or "").strip()
    if not question:
        raise _error("Ask your advisor something.")

    advice = advisor.ask(actor.contact_id, question)
    return f"--- Advice from your advisor ---\n{advice}"


def add_parser(subparsers, parser_cls):
    parser = subparsers.add_parser(
        "get-advice",
        help="ask your advisor how to do something (waits for the answer)",
    )
    parser.add_argument("question", help="what you want advice on")
    parser.set_defaults(_handler=cmd_get_advice)
