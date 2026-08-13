"""Running a turn of the Silicon session.

There is one persistent conversation, :data:`helpers.session.SILICON`, and
every message from every Carbon and Silicon lands in it. This module assembles
its prompt, hands the turn to :mod:`inference`, and gives the event loop back
whatever the turn produced. Which provider answers, how it streams, and how it
fails are all questions for ``inference/``.
"""
from inference import (
    TIMEOUT_MSG,
    Inference,
    ProviderTimeoutError,
    TurnRequest,
    brain,
    brain_order,
    is_rate_limit,
    parse_manager_output,
    provider_failed,
)
from helpers.session import SILICON
from prompts.loader import get_manager_prompt

INJECTED_PREFIX = (
    "[NEW MESSAGE — it arrived while you were working]\n\n"
)

INFERENCE = Inference(kind="manager")
# The advisor thinks on the same providers but keeps its own session and its
# own trail, so its calls never land in the session's log.
ADVISOR_INFERENCE = Inference(kind="advisor")


class ManagerTimeoutError(ProviderTimeoutError):
    """Kept for callers that catch the manager's own spelling of a timeout."""


def new_session(session_key=SILICON, brain=None):
    """Reset a session. Without a key, the Silicon session itself."""
    return INFERENCE.new_session(session_key, brain)


def manager_code(text, carbon_id, on_tools=None, on_progress=None, trace=None, env=None):
    """Invoke the configured brain and return its turn.

    Returns the historical ``(output, rate_limit, executed_tools)`` triple.
    """
    request = TurnRequest(
        text=text,
        contact_id=carbon_id,
        system_prompt=get_manager_prompt(),
        on_tools=on_tools,
        on_progress=on_progress,
        env=env,
        inject_key=carbon_id,
    )
    return INFERENCE.run_turn(request, trace=trace).as_tuple()


def run_agent(
    text,
    carbon_id,
    *,
    session_key,
    system_prompt,
    tag,
    on_progress=None,
    env=None,
):
    """Run a non-manager agent on the manager's configured brain order.

    The advisor is the caller: same providers, same fallback rules, its own
    session and instructions. Returns the agent's final text, or an empty
    string if every configured provider failed.
    """
    return ADVISOR_INFERENCE.run_agent(
        TurnRequest(
            text=text,
            contact_id=carbon_id,
            system_prompt=system_prompt,
            session_key=session_key,
            tag=tag,
            on_progress=on_progress,
            env=env,
        )
    )


__all__ = [
    "ADVISOR_INFERENCE",
    "INFERENCE",
    "INJECTED_PREFIX",
    "SILICON",
    "TIMEOUT_MSG",
    "ManagerTimeoutError",
    "brain",
    "brain_order",
    "is_rate_limit",
    "manager_code",
    "new_session",
    "parse_manager_output",
    "provider_failed",
    "run_agent",
]
