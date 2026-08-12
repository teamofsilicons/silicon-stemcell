"""The `iwantto` CLI — how a Silicon acts.

Managers, advisors, and workers do not return a batch of tool JSON and wait for
the Stemcell to act on it at the end of a turn.  They run `iwantto` as a shell
command mid-run, the command executes immediately against durable state and the
Interface daemon, and the result comes straight back on stdout.  A manager can
therefore send a message, read the reply, and act on it inside a single turn.

Identity is not a flag.  Every process Silicon spawns is registered in
:mod:`iwantto.actor` before it starts and handed a token, so `iwantto`
resolves "I" from who is running it rather than from what they claim to be.
"""

from iwantto.actor import (
    Actor,
    ActorError,
    actor_env,
    issue_run_env,
    register_actor,
    resolve_actor,
    revoke_actor,
)

__all__ = [
    "Actor",
    "ActorError",
    "actor_env",
    "issue_run_env",
    "register_actor",
    "resolve_actor",
    "revoke_actor",
]
