"""`iwantto work` and `iwantto remember`.

Work is the three-level model — work, task, subtask — that Glass does not
natively store, so what matters is that the local structure stays coherent and
that every change is pushed outward for the carbon to see.

Reminders are the only way a Silicon acts without being spoken to, and they are
its own: one session means "who is this for" has one answer. Glass only schedules
cron expressions, so a one-off reminder is a cron that matches one minute and is
deleted after it fires.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from helpers.silicon import SILICON
from iwantto import routing as routing_module
from iwantto.actor import MANAGER, Actor
from iwantto.cli import CommandError, build_parser
from iwantto.commands import remember as remember_module
from iwantto.commands import work as work_module


def _run(argv, actor):
    args = build_parser().parse_args(argv)
    return args._handler(args, actor)


MANAGER_ACTOR = Actor(
    kind=MANAGER, actor_id=SILICON, contact_id=SILICON, token="t"
)


class WorkTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.object(
            work_module, "WORK_FILE", os.path.join(self._temp.name, "work.json")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.glass = mock.patch.object(
            work_module, "_glass", return_value="Done. work_update"
        )
        self.glass_mock = self.glass.start()
        self.addCleanup(self.glass.stop)

    def _new_work(self):
        return _run(
            ["work", "--new", "--id", "market-research", "--name", "Market Research"],
            MANAGER_ACTOR,
        )

    def test_a_new_work_starts_running_and_is_pushed_to_glass(self):
        result = self._new_work()

        self.assertIn("Started work 'market-research'", result)
        action = self.glass_mock.call_args.args[1]
        self.assertEqual(action, "task/create")

    def test_a_duplicate_work_id_is_refused(self):
        self._new_work()
        with self.assertRaises(CommandError) as exc:
            self._new_work()

        self.assertIn("already exists", str(exc.exception))

    def test_touching_a_work_that_does_not_exist_is_refused(self):
        with self.assertRaises(CommandError) as exc:
            _run(["work", "--id", "nope", "--expand"], MANAGER_ACTOR)

        self.assertIn("No work with id 'nope'", str(exc.exception))

    def test_tasks_are_numbered_and_subtasks_carry_their_parent(self):
        self._new_work()
        first = _run(
            [
                "work", "--id", "market-research", "--add-task",
                "--title", "Research Healthify", "--description", "deep dive",
            ],
            MANAGER_ACTOR,
        )
        second = _run(
            [
                "work", "--id", "market-research", "--add-task",
                "--title", "Research Noom", "--description", "also popular",
            ],
            MANAGER_ACTOR,
        )
        subtask = _run(
            [
                "work", "--id", "market-research", "--task", "1",
                "--add-subtask", "--title", "Read Reddit",
                "--description", "threads about it",
            ],
            MANAGER_ACTOR,
        )

        self.assertIn("Added task 1", first)
        self.assertIn("Added task 2", second)
        self.assertIn("Added subtask 1.1 under task 1", subtask)
        # Glass keeps a flat todo list, so the parent shows in the title.
        self.assertEqual(
            self.glass_mock.call_args.args[3]["title"],
            "Research Healthify › Read Reddit",
        )

    def test_a_task_and_a_subtask_need_a_title_and_a_description(self):
        self._new_work()
        with self.assertRaises(CommandError) as exc:
            _run(
                ["work", "--id", "market-research", "--add-task", "--title", "x"],
                MANAGER_ACTOR,
            )
        self.assertIn("--title and a --description", str(exc.exception))

    def test_starting_and_ending_records_the_note_and_the_state(self):
        self._new_work()
        _run(
            [
                "work", "--id", "market-research", "--add-task",
                "--title", "Research", "--description", "d",
            ],
            MANAGER_ACTOR,
        )
        started = _run(
            ["work", "--id", "market-research", "--task", "1", "--start", "on it"],
            MANAGER_ACTOR,
        )
        ended = _run(
            [
                "work", "--id", "market-research", "--task", "1",
                "--end", "learned a lot",
            ],
            MANAGER_ACTOR,
        )
        expanded = _run(["work", "--id", "market-research", "--expand"], MANAGER_ACTOR)

        self.assertIn("Task 1 started", started)
        self.assertIn("Task 1 finished", ended)
        self.assertIn("start: on it", expanded)
        self.assertIn("end: learned a lot", expanded)
        self.assertIn("[completed] task 1", expanded)

    def test_expand_shows_the_whole_tree(self):
        self._new_work()
        _run(
            [
                "work", "--id", "market-research", "--add-task",
                "--title", "Research", "--description", "d",
            ],
            MANAGER_ACTOR,
        )
        _run(
            [
                "work", "--id", "market-research", "--task", "1",
                "--add-subtask", "--title", "Reddit", "--description", "s",
            ],
            MANAGER_ACTOR,
        )
        expanded = _run(["work", "--id", "market-research", "--expand"], MANAGER_ACTOR)

        self.assertIn("market-research — Market Research", expanded)
        self.assertIn("task 1 — Research", expanded)
        self.assertIn("subtask 1.1 — Reddit", expanded)

    def test_updates_and_blockers_reach_the_carbon(self):
        self._new_work()
        update = _run(
            [
                "work", "--id", "market-research", "--dispatch-update",
                "--title", "Part 1 done", "--description", "here is what happened",
            ],
            MANAGER_ACTOR,
        )
        self.assertEqual(self.glass_mock.call_args.args[1], "milestone")
        self.assertIn("Update sent to your carbon", update)

        blocker = _run(
            [
                "work", "--id", "market-research", "--blocker",
                "--title", "Where do I publish?", "--description", "need a decision",
            ],
            MANAGER_ACTOR,
        )
        self.assertEqual(self.glass_mock.call_args.args[1], "blocker/create")
        self.assertIn("They have been notified", blocker)

    def test_completing_a_work_moves_it_out_of_active(self):
        self._new_work()
        self.assertIn("market-research", _run(["work", "--active"], MANAGER_ACTOR))

        _run(
            [
                "work", "--id", "market-research", "--completed",
                "--title", "DONE", "--description", "finishing note",
            ],
            MANAGER_ACTOR,
        )

        self.assertEqual(self.glass_mock.call_args.args[1], "task/complete")
        self.assertIn("No active work", _run(["work", "--active"], MANAGER_ACTOR))
        self.assertIn("market-research", _run(["work", "--last", "10"], MANAGER_ACTOR))

    def test_listing_work_takes_no_owner_because_there_is_only_one(self):
        """`--by` filtered work by whose manager started it. Nobody else has one."""
        self._new_work()
        self.assertIn("market-research", _run(["work", "--active"], MANAGER_ACTOR))
        with self.assertRaises(CommandError):
            _run(["work", "--active", "--by", "carbon-b"], MANAGER_ACTOR)

    def _failing_glass(self):
        """Stop mocking _glass so its own failure handling runs."""
        self.glass.stop()
        self.addCleanup(self.glass.start)
        return mock.patch(
            "interface.work.execute_work_update",
            return_value="Error: work_update failed: Contact has no Interface room.",
        )

    def test_a_failed_push_is_reported_and_changes_nothing(self):
        """A work the carbon cannot see must not be reported as created."""
        with self._failing_glass():
            with self.assertRaises(CommandError) as exc:
                self._new_work()

        self.assertIn("Your carbon was not updated", str(exc.exception))
        self.assertIn("Nothing was changed", str(exc.exception))
        self.assertEqual(work_module._works(), {})

    def test_a_failed_task_push_rolls_the_task_back(self):
        self._new_work()
        with self._failing_glass():
            with self.assertRaises(CommandError):
                _run(
                    [
                        "work", "--id", "market-research", "--add-task",
                        "--title", "Doomed", "--description", "d",
                    ],
                    MANAGER_ACTOR,
                )

        work = work_module._works()["market-research"]
        self.assertEqual(work["tasks"], {})
        # The number is released, so the next task is still task 1.
        self.assertEqual(work["next_task_number"], 1)

    def test_a_failed_transition_leaves_the_previous_state(self):
        self._new_work()
        _run(
            [
                "work", "--id", "market-research", "--add-task",
                "--title", "Research", "--description", "d",
            ],
            MANAGER_ACTOR,
        )
        with self._failing_glass():
            with self.assertRaises(CommandError):
                _run(
                    [
                        "work", "--id", "market-research", "--task", "1",
                        "--start", "on it",
                    ],
                    MANAGER_ACTOR,
                )

        task = work_module._works()["market-research"]["tasks"]["1"]
        self.assertEqual(task["state"], work_module.NOT_STARTED)
        self.assertEqual(task["start_note"], "")

    def test_renaming_keeps_the_id_and_pushes_the_new_name(self):
        self._new_work()
        result = _run(
            ["work", "--id", "market-research", "--name", "Market Research on All"],
            MANAGER_ACTOR,
        )

        self.assertEqual(self.glass_mock.call_args.args[1], "task/update")
        self.assertIn("Renamed 'market-research'", result)


class RememberTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.object(
            remember_module,
            "REMINDERS_FILE",
            os.path.join(self._temp.name, "reminders.json"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        own = mock.patch.object(
            remember_module,
            "_own_target",
            return_value=[{"kind": "silicon", "id": "me"}],
        )
        own.start()
        self.addCleanup(own.stop)

    def test_a_relative_reminder_becomes_a_one_shot_cron(self):
        with mock.patch.object(
            remember_module, "_create_glass_cron", return_value="cron-1"
        ) as create:
            result = _run(
                ["remember", "--in", "2h", "--text", "check on it"],
                MANAGER_ACTOR,
            )

        trigger = create.call_args.args[0]
        self.assertRegex(trigger, r"^\d+ \d+ \d+ \d+ \*$")
        self.assertEqual(create.call_args.args[1], "check on it")
        self.assertIn("Reminder r-", result)
        self.assertIn("once at", result)

    def test_a_reminder_is_for_the_silicon_itself(self):
        """It takes no target, and it is stored against our own Glass identity.

        Naming somebody was meaningful when there was a manager per contact and
        one could poke another\'s. There is one session now.
        """
        with self.assertRaises(CommandError):
            _run(["remember", "carbon-b", "--in", "2h", "--text", "x"], MANAGER_ACTOR)

        with mock.patch.object(
            remember_module, "_create_glass_cron", return_value="cron-1"
        ) as create:
            _run(["remember", "--in", "2h", "--text", "x"], MANAGER_ACTOR)

        self.assertEqual(
            create.call_args.args[2], [{"kind": "silicon", "id": "me"}]
        )

    def test_a_reminder_needs_an_identity_it_can_outlive_a_restart_with(self):
        """No Glass identity means no durable cron, and that is worth refusing.

        Storing it locally only would look like it worked and then vanish on the
        next reinstall, because reminders are not in `.backupsilicon`.
        """
        with (
            mock.patch.object(
                remember_module,
                "_own_target",
                side_effect=CommandError("I do not know my own Glass identity yet"),
            ),
            self.assertRaises(CommandError) as refused,
        ):
            _run(["remember", "--in", "2h", "--text", "x"], MANAGER_ACTOR)

        self.assertIn("my own Glass identity", str(refused.exception))

    def test_unsupported_units_are_refused(self):
        for value in ("30s", "2w", "1y", "banana"):
            with self.subTest(value=value):
                with self.assertRaises(CommandError) as exc:
                    _run(["remember", "--in", value, "--text", "x"], MANAGER_ACTOR)
                self.assertIn("m, h, or d", str(exc.exception))

    def test_a_recurring_reminder_keeps_its_cron_and_timezone(self):
        with mock.patch.object(
            remember_module, "_create_glass_cron", return_value="cron-2"
        ) as create:
            _run(
                [
                    "remember", "--cron", "0 9 * * 1-5",
                    "--tz", "Asia/Dubai", "--text", "standup",
                ],
                MANAGER_ACTOR,
            )

        self.assertEqual(create.call_args.args[0], "0 9 * * 1-5")
        self.assertEqual(create.call_args.args[3], "Asia/Dubai")

    def test_a_reminder_must_say_when(self):
        with self.assertRaises(CommandError) as exc:
            _run(["remember", "--text", "x"], MANAGER_ACTOR)
        self.assertIn("Say when", str(exc.exception))

    def test_a_reminder_can_be_deleted_by_id(self):
        with mock.patch.object(
            remember_module, "_create_glass_cron", return_value="cron-1"
        ):
            created = _run(
                ["remember", "--cron", "0 9 * * *", "--text", "x"], MANAGER_ACTOR
            )
        reminder_id = created.split()[1]

        with mock.patch.object(remember_module, "_delete_glass_cron") as delete:
            result = _run(
                ["remember", "--id", reminder_id, "--delete"], MANAGER_ACTOR
            )

        delete.assert_called_once_with("cron-1")
        self.assertIn("Deleted reminder", result)
        self.assertNotIn(reminder_id, remember_module._reminders())

    def test_a_fired_one_shot_is_reaped_and_a_recurring_one_is_not(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        remember_module._store(
            "r-past",
            {"cron_id": "c1", "one_shot": True, "fire_at": past.timestamp()},
        )
        remember_module._store(
            "r-future",
            {"cron_id": "c2", "one_shot": True, "fire_at": future.timestamp()},
        )
        remember_module._store(
            "r-recurring",
            {"cron_id": "c3", "one_shot": False, "trigger": "0 9 * * *"},
        )

        with mock.patch.object(remember_module, "_delete_glass_cron") as delete:
            reaped = remember_module.reap_fired_reminders()

        self.assertEqual(reaped, 1)
        delete.assert_called_once_with("c1")
        self.assertEqual(
            sorted(remember_module._reminders()), ["r-future", "r-recurring"]
        )

    def test_listing_shows_every_reminder_because_they_are_all_mine(self):
        with mock.patch.object(
            remember_module, "_create_glass_cron", return_value="cron-1"
        ):
            self.assertIn("no reminders", _run(["remember", "--list"], MANAGER_ACTOR))
            _run(["remember", "--cron", "0 9 * * *", "--text", "standup"], MANAGER_ACTOR)
            _run(["remember", "--in", "3h", "--text", "chase the worker"], MANAGER_ACTOR)

        listed = _run(["remember", "--list"], MANAGER_ACTOR)
        self.assertIn("standup", listed)
        self.assertIn("chase the worker", listed)


if __name__ == "__main__":
    unittest.main()
