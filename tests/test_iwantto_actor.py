"""Who `iwantto` thinks is running it.

Identity is the whole basis for routing: whether `iwantto send shubham` goes
straight to shubham or through shubham's manager depends entirely on which
manager is asking. These tests hold that the answer comes from a token issued
per run, that it cannot be guessed or inherited, and that it stops working when
the run ends.
"""
import os
import tempfile
import unittest
from unittest import mock

from core.iwantto import actor as actor_module
from core.iwantto.actor import (
    ADVISOR,
    Actor,
    ActorError,
    MANAGER,
    WORKER,
    issue_run_env,
    register_actor,
    resolve_actor,
    revoke_actor,
)


class ActorIdentityTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.object(
            actor_module,
            "ACTORS_FILE",
            os.path.join(self._temp.name, "actors.json"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_registered_run_resolves_to_its_own_identity(self):
        token = register_actor(MANAGER, "carbon-a", "carbon-a")

        resolved = resolve_actor({actor_module.TOKEN_ENV: token})

        self.assertEqual(resolved.kind, MANAGER)
        self.assertEqual(resolved.contact_id, "carbon-a")
        self.assertTrue(resolved.is_manager)
        self.assertTrue(resolved.acts_as_manager)

    def test_an_advisor_shares_its_managers_contact(self):
        token = register_actor(ADVISOR, "carbon-a", "carbon-a")

        resolved = resolve_actor({actor_module.TOKEN_ENV: token})

        self.assertTrue(resolved.is_advisor)
        self.assertTrue(resolved.acts_as_manager)
        self.assertEqual(resolved.contact_id, "carbon-a")

    def test_a_worker_knows_the_manager_it_works_for(self):
        token = register_actor(
            WORKER, "researcher", "carbon-a", worker_type="browser"
        )

        resolved = resolve_actor({actor_module.TOKEN_ENV: token})

        self.assertTrue(resolved.is_worker)
        self.assertFalse(resolved.acts_as_manager)
        self.assertEqual(resolved.actor_id, "researcher")
        self.assertEqual(resolved.contact_id, "carbon-a")
        self.assertIn("browser worker", resolved.describe())

    def test_an_unidentified_caller_is_refused_rather_than_guessed(self):
        with self.assertRaises(ActorError) as missing:
            resolve_actor({})
        self.assertIn("could not tell who is running it", str(missing.exception))

        with self.assertRaises(ActorError) as unknown:
            resolve_actor({actor_module.TOKEN_ENV: "not-a-real-token"})
        self.assertIn("unknown or has expired", str(unknown.exception))

    def test_a_revoked_token_stops_resolving(self):
        token = register_actor(MANAGER, "carbon-a", "carbon-a")
        revoke_actor(token)

        with self.assertRaises(ActorError):
            resolve_actor({actor_module.TOKEN_ENV: token})

    def test_an_expired_token_stops_resolving(self):
        token = register_actor(MANAGER, "carbon-a", "carbon-a", ttl_seconds=-1)

        with self.assertRaises(ActorError):
            resolve_actor({actor_module.TOKEN_ENV: token})

    def test_claiming_another_kind_needs_that_kinds_token(self):
        """A leaked variable cannot promote a worker into a manager."""
        worker_token = register_actor(
            WORKER, "researcher", "carbon-a", worker_type="terminal"
        )

        resolved = resolve_actor({
            actor_module.TOKEN_ENV: worker_token,
            # Deliberately lying about kind and contact.
            actor_module.KIND_ENV: MANAGER,
            actor_module.ID_ENV: "carbon-b",
            actor_module.CONTACT_ENV: "carbon-b",
        })

        self.assertTrue(resolved.is_worker)
        self.assertEqual(resolved.contact_id, "carbon-a")

    def test_two_runs_never_share_a_token(self):
        first = register_actor(MANAGER, "carbon-a", "carbon-a")
        second = register_actor(MANAGER, "carbon-b", "carbon-b")

        self.assertNotEqual(first, second)
        self.assertEqual(
            resolve_actor({actor_module.TOKEN_ENV: first}).contact_id, "carbon-a"
        )
        self.assertEqual(
            resolve_actor({actor_module.TOKEN_ENV: second}).contact_id, "carbon-b"
        )


class RunEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.object(
            actor_module,
            "ACTORS_FILE",
            os.path.join(self._temp.name, "actors.json"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_spawned_run_gets_a_complete_environment(self):
        base = {"PATH": "/usr/bin", "SILICON_DATA_ROOT": "/data"}

        token, env = issue_run_env(
            MANAGER, "carbon-a", "carbon-a", base_env=base
        )

        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["SILICON_DATA_ROOT"], "/data")
        self.assertEqual(env[actor_module.TOKEN_ENV], token)
        self.assertEqual(env[actor_module.KIND_ENV], MANAGER)
        self.assertEqual(env[actor_module.CONTACT_ENV], "carbon-a")

    def test_a_parents_identity_never_survives_into_its_child(self):
        """A manager's token must not reach the worker it spawns.

        If it did, the worker would resolve as the manager and could send a
        message as one Carbon's manager while working for another.
        """
        manager_token, manager_env = issue_run_env(
            MANAGER, "carbon-a", "carbon-a", base_env={"PATH": "/usr/bin"}
        )

        worker_token, worker_env = issue_run_env(
            WORKER,
            "researcher",
            "carbon-a",
            worker_type="browser",
            base_env=manager_env,
        )

        self.assertNotEqual(worker_token, manager_token)
        self.assertEqual(worker_env[actor_module.TOKEN_ENV], worker_token)
        self.assertEqual(worker_env[actor_module.KIND_ENV], WORKER)
        self.assertEqual(worker_env[actor_module.ID_ENV], "researcher")
        self.assertEqual(
            resolve_actor(worker_env).kind,
            WORKER,
        )

    def test_the_registry_is_bounded(self):
        for index in range(actor_module.MAX_ACTORS + 20):
            register_actor(MANAGER, f"carbon-{index}", f"carbon-{index}")

        from helpers.state import read_json

        state = read_json(actor_module.ACTORS_FILE, {"actors": {}})
        self.assertLessEqual(len(state["actors"]), actor_module.MAX_ACTORS)


class ActorDescriptionTest(unittest.TestCase):
    def test_describe_names_the_contact_a_run_answers_to(self):
        self.assertEqual(
            Actor(kind=MANAGER, actor_id="carbon-a", contact_id="carbon-a").describe(),
            "manager of `carbon-a`",
        )
        self.assertEqual(
            Actor(
                kind=WORKER,
                actor_id="w1",
                contact_id="carbon-a",
                worker_type="writer",
            ).describe(),
            "writer worker `w1` working for the manager of `carbon-a`",
        )


if __name__ == "__main__":
    unittest.main()
