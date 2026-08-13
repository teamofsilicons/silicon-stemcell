"""Who is running `iwantto`.

Every process the Stemcell spawns — a session turn, an advisor turn, a worker
run — is registered here before it starts and handed a token in its
environment. The CLI resolves that token back to an actor, so `iwantto send
shubham --text "..."` knows whether "I" is the Silicon, which speaks for itself,
or one of its workers, which does not.

The token is the identity, not the variable names around it.  A stale or copied
`SILICON_ACTOR_*` value cannot name an actor it was never issued for, and a
token stops resolving the moment its run is revoked.  Tokens also carry a TTL so
a process killed without cleanup cannot leave a usable identity behind.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass

from helpers.paths import DATA_ROOT, STATE_DIR
from helpers.state import read_json, update_json

PROJECT_ROOT = os.fspath(DATA_ROOT)
ACTORS_FILE = os.path.join(
    os.fspath(STATE_DIR), "actors.json"
)

TOKEN_ENV = "SILICON_ACTOR_TOKEN"
KIND_ENV = "SILICON_ACTOR_KIND"
ID_ENV = "SILICON_ACTOR_ID"
CONTACT_ENV = "SILICON_ACTOR_CONTACT"

MANAGER = "manager"
ADVISOR = "advisor"
WORKER = "worker"
VALID_KINDS = (MANAGER, ADVISOR, WORKER)

# A manager turn is bounded by MANAGER_TIMEOUT (30 min) and an advisor turn is
# shorter, so two hours is a backstop rather than a working lifetime. Workers
# can legitimately run for hours, so they get a full day before the registry
# forgets them.
DEFAULT_TTL_SECONDS = {
    MANAGER: 2 * 60 * 60,
    ADVISOR: 2 * 60 * 60,
    WORKER: 24 * 60 * 60,
}

# Bounds the registry if revocation is ever skipped often enough to matter.
MAX_ACTORS = 512


class ActorError(RuntimeError):
    """The caller could not be identified as a registered Silicon actor."""


@dataclass(frozen=True)
class Actor:
    """A resolved caller of `iwantto`."""

    kind: str
    actor_id: str
    contact_id: str
    worker_type: str = ""
    token: str = ""

    @property
    def is_manager(self) -> bool:
        return self.kind == MANAGER

    @property
    def is_advisor(self) -> bool:
        return self.kind == ADVISOR

    @property
    def is_worker(self) -> bool:
        return self.kind == WORKER

    @property
    def acts_as_manager(self) -> bool:
        """Advisors share the Silicon's "I" — same contact, same voice."""
        return self.kind in (MANAGER, ADVISOR)

    def describe(self) -> str:
        if self.is_worker:
            label = f"{self.worker_type or 'worker'} worker `{self.actor_id}`"
            return f"{label} working for `{self.contact_id}`"
        return f"{self.kind} of `{self.contact_id}`"


def _now() -> float:
    return time.time()


def _default_state() -> dict:
    return {"version": 1, "actors": {}}


def _prune(actors: dict, now: float) -> None:
    """Drop expired entries, then the oldest if the registry is still over cap."""
    for token, entry in list(actors.items()):
        if not isinstance(entry, dict):
            actors.pop(token, None)
            continue
        expires_at = entry.get("expires_at")
        if not isinstance(expires_at, (int, float)) or expires_at <= now:
            actors.pop(token, None)
    if len(actors) <= MAX_ACTORS:
        return
    ordered = sorted(
        actors.items(),
        key=lambda item: float(item[1].get("created_at") or 0.0),
    )
    for token, _entry in ordered[: len(actors) - MAX_ACTORS]:
        actors.pop(token, None)


def register_actor(
    kind: str,
    actor_id: str,
    contact_id: str,
    *,
    worker_type: str = "",
    ttl_seconds: float | None = None,
) -> str:
    """Register a run and return the token that identifies it.

    Pass the token to the spawned process through :func:`actor_env`, and revoke
    it with :func:`revoke_actor` when the run ends.
    """
    kind = str(kind or "").strip().lower()
    if kind not in VALID_KINDS:
        raise ActorError(f"Unknown actor kind: {kind!r}")
    actor_id = str(actor_id or "").strip()
    contact_id = str(contact_id or "").strip()
    if not actor_id or not contact_id:
        raise ActorError("An actor needs both an id and a contact id")

    token = secrets.token_urlsafe(32)
    now = _now()
    ttl = (
        float(ttl_seconds)
        if ttl_seconds is not None
        else float(DEFAULT_TTL_SECONDS[kind])
    )
    entry = {
        "kind": kind,
        "actor_id": actor_id,
        "contact_id": contact_id,
        "worker_type": str(worker_type or ""),
        "created_at": now,
        "expires_at": now + ttl,
        "pid": os.getpid(),
    }

    def update(state):
        actors = state.setdefault("actors", {})
        # Insert first, then prune, so the cap counts the new entry. The entry
        # just added is the newest and so is never the one evicted.
        actors[token] = entry
        _prune(actors, now)

    update_json(ACTORS_FILE, _default_state(), update)
    return token


def revoke_actor(token: str) -> None:
    """Retire a token. Safe to call twice, and safe to call on an unknown token."""
    token = str(token or "")
    if not token:
        return

    def update(state):
        state.setdefault("actors", {}).pop(token, None)

    try:
        update_json(ACTORS_FILE, _default_state(), update)
    except OSError:
        # The TTL is the backstop; a failed revoke must never break a run.
        pass


def actor_env(token: str, actor: Actor | None = None) -> dict:
    """The environment a spawned run needs so `iwantto` can identify it.

    Only the token is authoritative. The other variables exist so a Silicon
    reading its own environment can see who it is without a lookup.
    """
    env = {TOKEN_ENV: str(token or "")}
    if actor is not None:
        env[KIND_ENV] = actor.kind
        env[ID_ENV] = actor.actor_id
        env[CONTACT_ENV] = actor.contact_id
    return env


def issue_run_env(
    kind: str,
    actor_id: str,
    contact_id: str,
    *,
    worker_type: str = "",
    base_env: dict | None = None,
    ttl_seconds: float | None = None,
) -> tuple[str, dict]:
    """Register a run and return ``(token, environment)`` for its subprocess.

    The environment is a complete copy, so it can be handed straight to Popen.
    Any inherited actor variables are dropped first: a manager's token must
    never survive into the worker it spawns, or the worker would act as the
    manager and route another Carbon's message as its own.
    """
    env = dict(os.environ if base_env is None else base_env)
    for name in (TOKEN_ENV, KIND_ENV, ID_ENV, CONTACT_ENV):
        env.pop(name, None)
    token = register_actor(
        kind,
        actor_id,
        contact_id,
        worker_type=worker_type,
        ttl_seconds=ttl_seconds,
    )
    env.update(
        actor_env(
            token,
            Actor(
                kind=kind,
                actor_id=actor_id,
                contact_id=contact_id,
                worker_type=worker_type,
            ),
        )
    )
    return token, env


def lookup_token(token: str) -> Actor | None:
    """Resolve a token to its actor, or None if it is unknown or expired."""
    token = str(token or "")
    if not token:
        return None
    state = read_json(ACTORS_FILE, _default_state())
    entry = (state.get("actors") or {}).get(token)
    if not isinstance(entry, dict):
        return None
    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, (int, float)) or expires_at <= _now():
        return None
    kind = str(entry.get("kind") or "")
    if kind not in VALID_KINDS:
        return None
    return Actor(
        kind=kind,
        actor_id=str(entry.get("actor_id") or ""),
        contact_id=str(entry.get("contact_id") or ""),
        worker_type=str(entry.get("worker_type") or ""),
        token=token,
    )


def resolve_actor(env: dict | None = None) -> Actor:
    """Identify the caller from the environment, or explain why we cannot.

    Raises :class:`ActorError` rather than guessing. An unidentified caller has
    no "I" to resolve, so every routing decision below it would be a guess too.
    """
    environ = os.environ if env is None else env
    token = str(environ.get(TOKEN_ENV) or "").strip()
    if not token:
        raise ActorError(
            "iwantto could not tell who is running it: no "
            f"{TOKEN_ENV} in this environment. Run iwantto from a manager, "
            "advisor, or worker process started by Silicon."
        )
    actor = lookup_token(token)
    if actor is None:
        raise ActorError(
            "iwantto could not tell who is running it: this "
            f"{TOKEN_ENV} is unknown or has expired. The run that owned it has "
            "already finished."
        )
    return actor
