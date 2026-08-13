"""The advisor, and the heartbeat that wakes a Silicon nobody messaged.

There is one advisor for the one session. It holds its own conversation so
advice accumulates across a working session, but not indefinitely: a two-hour
gap or a day-old session means it has moved on and should start clean.

The heartbeat is what makes a Silicon capable of acting rather than only
reacting, so it must fire on its own schedule and arrive carrying the work the
session already has running. One beat, not one per contact — a beat belongs to
nobody in particular.
"""
import os
import tempfile
import time
import unittest
from unittest import mock

from helpers.silicon import SILICON
from manager import advisor as advisor_module
from manager.advisor import heartbeat as heartbeat_module


class AdvisorSessionTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.object(
            advisor_module,
            "ADVISOR_STATE_FILE",
            os.path.join(self._temp.name, "advisors.json"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_first_question_opens_a_session(self):
        self.assertEqual(
            advisor_module._should_rotate({}, time.time()),
            "first advice given",
        )

    def test_a_two_hour_gap_starts_a_fresh_session(self):
        now = time.time()
        recent = {"last_invoked_at": now - 60, "session_started_at": now - 120}
        stale = {
            "last_invoked_at": now - (2 * 60 * 60 + 60),
            "session_started_at": now - (3 * 60 * 60),
        }

        self.assertEqual(advisor_module._should_rotate(recent, now), "")
        self.assertIn("2 hours", advisor_module._should_rotate(stale, now))

    def test_a_session_never_outlives_a_day(self):
        now = time.time()
        busy_but_old = {
            "last_invoked_at": now - 60,
            "session_started_at": now - (24 * 60 * 60 + 60),
        }

        self.assertIn("24 hours", advisor_module._should_rotate(busy_but_old, now))

    def test_the_advisor_reads_its_three_files_in_order(self):
        self.assertEqual(
            advisor_module.ADVISOR_PROMPT_FILES,
            ("INDEX.md", "IWANTTO_CLI_REFERENCE.md", "ADVISOR.md"),
        )
        prompt = advisor_module.build_prompt()

        from prompts import loader

        # INDEX.md lists the other prompts by path, so the label alone appears
        # more than once. The rendered block is what has to be in order.
        positions = [
            prompt.index(loader._read_prompt(name))
            for name in advisor_module.ADVISOR_PROMPT_FILES
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("advisor to this Silicon", prompt)
        self.assertIn("do not act on its behalf", prompt)

    def test_the_advisor_keeps_its_own_conversation(self):
        self.assertEqual(
            advisor_module.session_key(), f"advisor__{SILICON}"
        )

    def test_asking_runs_the_agent_under_an_advisor_identity_and_revokes_it(self):
        with (
            mock.patch.object(
                advisor_module, "_rotate_session"
            ) as rotate,
            mock.patch(
                "iwantto.actor.issue_run_env",
                return_value=("advisor-token", {"SILICON_ACTOR_TOKEN": "advisor-token"}),
            ) as issue,
            mock.patch("iwantto.actor.revoke_actor") as revoke,
            mock.patch("manager.run_agent", return_value="Delegate it.") as run_agent,
        ):
            advice = advisor_module.ask("carbon-a", "should I do this myself?")

        rotate.assert_called_once()
        self.assertEqual(issue.call_args.args, ("advisor", "carbon-a", "carbon-a"))
        revoke.assert_called_once_with("advisor-token")
        self.assertEqual(run_agent.call_args.kwargs["session_key"], "advisor__carbon-a")
        self.assertEqual(run_agent.call_args.kwargs["tag"], "advisor:carbon-a")
        self.assertEqual(advice, "Delegate it.")

    def test_the_advisor_subprocess_receives_its_identity(self):
        """Without the environment an advisor could not run iwantto at all."""
        with (
            mock.patch.object(advisor_module, "_rotate_session"),
            mock.patch(
                "iwantto.actor.issue_run_env",
                return_value=("advisor-token", {"SILICON_ACTOR_TOKEN": "advisor-token"}),
            ),
            mock.patch("iwantto.actor.revoke_actor"),
            mock.patch("manager.run_agent", return_value="Advice.") as run_agent,
        ):
            advisor_module.ask("carbon-a", "what now?")

        self.assertEqual(
            run_agent.call_args.kwargs["env"],
            {"SILICON_ACTOR_TOKEN": "advisor-token"},
        )

    def test_run_agent_passes_the_environment_to_the_provider(self):
        import inference
        import manager

        env = {"SILICON_ACTOR_TOKEN": "advisor-token"}
        with (
            mock.patch.object(manager.INFERENCE, "_order", ["claude"]),
            mock.patch.object(
                inference.get_provider("claude"),
                "run_turn",
                return_value=inference.TurnResult("advice"),
            ) as run_turn,
        ):
            manager.run_agent(
                "q",
                "carbon-a",
                session_key="advisor__carbon-a",
                system_prompt="p",
                tag="advisor:carbon-a",
                env=env,
            )

        self.assertEqual(run_turn.call_args.args[0].env, env)

    def test_a_provider_failure_is_reported_rather_than_faked(self):
        with (
            mock.patch.object(advisor_module, "_rotate_session"),
            mock.patch(
                "iwantto.actor.issue_run_env", return_value=("t", {})
            ),
            mock.patch("iwantto.actor.revoke_actor"),
            mock.patch("manager.run_agent", return_value=""),
        ):
            advice = advisor_module.ask("carbon-a", "what now?")

        self.assertIn("could not be reached", advice)
        self.assertIn("Decide without it", advice)

    def test_an_empty_question_is_refused(self):
        self.assertTrue(advisor_module.ask(SILICON, "   ").startswith("Error"))
        self.assertTrue(advisor_module.ask(question="").startswith("Error"))


class AdvisorHeartbeatTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.object(
            advisor_module,
            "ADVISOR_STATE_FILE",
            os.path.join(self._temp.name, "advisors.json"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_never_run_advisor_waits_a_full_interval_before_its_first_beat(self):
        self.assertFalse(advisor_module.heartbeat_due())
        self.assertFalse(advisor_module.heartbeat_due())

    def test_an_advisor_silent_for_five_hours_is_due(self):
        stale = time.time() - (5 * 60 * 60 + 60)
        advisor_module.update_json(
            advisor_module.ADVISOR_STATE_FILE,
            advisor_module._default_state(),
            lambda state: state.setdefault("advisors", {}).setdefault(
                SILICON, {}
            ).update({"last_heartbeat_at": stale}),
        )

        self.assertTrue(advisor_module.heartbeat_due())

    def test_heartbeat_advice_reaches_the_session_marked_as_the_advisors(self):
        with (
            mock.patch.object(advisor_module, "heartbeat_due", return_value=True),
            mock.patch.object(
                advisor_module, "ask", return_value="Chase that worker."
            ) as ask,
        ):
            contexts = advisor_module.run_heartbeats()

        ask.assert_called_once_with(
            SILICON, advisor_module.HEARTBEAT_PROMPT, heartbeat=True
        )
        self.assertEqual(list(contexts), [SILICON])
        self.assertIn("[Message from your Advisor]", contexts[SILICON])
        self.assertIn("Chase that worker.", contexts[SILICON])

    def test_a_heartbeat_asks_the_advisor_something(self):
        """The wording is the operator's to change; that there is one is not."""
        self.assertTrue(advisor_module.HEARTBEAT_PROMPT.strip())


class ManagerHeartbeatTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.object(
            heartbeat_module,
            "HEARTBEAT_STATE_FILE",
            os.path.join(self._temp.name, "heartbeats.json"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_interval_is_thirteen_minutes(self):
        self.assertEqual(heartbeat_module.INTERVAL_SECONDS, 13 * 60)
        self.assertEqual(
            heartbeat_module.BEAT_MESSAGE,
            "congrats, your heart is beating, make it count!",
        )

    def test_a_fresh_instance_starts_its_clock_instead_of_beating_immediately(self):
        self.assertIsNone(heartbeat_module.check_manager_heartbeats())
        self.assertIsNone(heartbeat_module.check_manager_heartbeats())

    def test_a_session_silent_for_the_interval_gets_one_beat(self):
        stale = time.time() - (13 * 60 + 1)
        heartbeat_module.update_json(
            heartbeat_module.HEARTBEAT_STATE_FILE,
            heartbeat_module._default_state(),
            lambda state: state.setdefault("contacts", {}).setdefault(
                SILICON, {}
            ).update({"last_beat_at": stale}),
        )

        with mock.patch.object(
            heartbeat_module, "_active_work_section", return_value=""
        ):
            contexts = heartbeat_module.check_manager_heartbeats()
            # Immediately after beating, it is not due again.
            again = heartbeat_module.check_manager_heartbeats()

        self.assertEqual(list(contexts), [SILICON])
        self.assertIn("[HEARTBEAT]", contexts[SILICON])
        self.assertIn(heartbeat_module.BEAT_MESSAGE, contexts[SILICON])
        self.assertIsNone(again)

    def test_a_beat_names_nobody_so_it_shows_up_in_no_ones_chat(self):
        """A heartbeat has no sender, so it must not carry an origin envelope.

        Untargeted frames fan out to whoever a turn is answering. A beat is
        answering nobody, and a spinner appearing unprompted in five chats every
        thirteen minutes is exactly what that would cause.
        """
        from helpers.silicon import origins_in

        with mock.patch.object(
            heartbeat_module, "_active_work_section", return_value=""
        ):
            self.assertEqual(origins_in(heartbeat_module.build_context()), [])

    def test_each_beat_carries_the_managers_own_active_work(self):
        work = [
            (
                "market-research",
                {
                    "name": "Market Research",
                    "state": "in_progress",
                    "owner_contact_id": SILICON,
                    "tasks": {},
                    "created_at_iso": "2026-08-09T00:00:00Z",
                },
            )
        ]
        with mock.patch(
            "iwantto.commands.work.active_works", return_value=work
        ) as active:
            context = heartbeat_module.build_context()

        active.assert_called_once_with(SILICON)
        self.assertIn("iwantto work --active", context)
        self.assertIn("market-research", context)

    def test_a_manager_with_no_work_is_told_so_plainly(self):
        with mock.patch(
            "iwantto.commands.work.active_works", return_value=[]
        ):
            context = heartbeat_module.build_context()

        self.assertIn("no active work right now", context)


class EventLoopWiringTest(unittest.TestCase):
    def test_the_heartbeats_and_the_reaper_are_on_the_event_loop(self):
        from manager import loop_config as config

        handlers = {entry["name"]: entry for entry in config.EVENT_LOOP}

        self.assertIn("check_manager_heartbeats", handlers)
        self.assertIn("check_advisor_heartbeats", handlers)
        self.assertIn("reap_reminders", handlers)
        # Checked every minute so the 13-minute cadence survives a restart
        # rather than resetting to the handler's own interval.
        self.assertEqual(handlers["check_manager_heartbeats"]["interval_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
