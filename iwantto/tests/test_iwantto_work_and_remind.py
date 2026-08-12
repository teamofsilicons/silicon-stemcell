"""`iwantto work` and `iwantto remind`.

Work is the three-level model — work, task, subtask — that Glass does not
natively store, so what matters is that the local structure stays coherent and
that every change is pushed outward for the carbon to see.

Reminders are the only way a Silicon acts without being spoken to. Glass only
schedules cron expressions, so a one-off reminder is a cron that matches one
minute and is deleted after it fires.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from iwantto import routing as routing_module
from iwantto.actor import MANAGER, Actor
from iwantto.cli import CommandError, build_parser
from iwantto.commands import remind as remind_module
from iwantto.commands import work as work_module


def _run(argv, actor):
    args = build_parser().parse_args(argv)
    return args._handler(args, actor)


MANAGER_ACTOR = Actor(
    kind=MANAGER, actor_id="carbon-a", contact_id="carbon-a", token="t"
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

    def test_work_can_be_listed_by_the_manager_that_started_it(self):
        self._new_work()
        contacts = {
            "carbon-a": {"contact_type": "carbon"},
            "carbon-b": {"contact_type": "carbon"},
        }
        with mock.patch.object(
            routing_module, "_local_contacts", return_value=contacts
        ):
            mine = _run(["work", "--active", "--by", "carbon-a"], MANAGER_ACTOR)
            theirs = _run(["work", "--active", "--by", "carbon-b"], MANAGER_ACTOR)

        self.assertIn("market-research", mine)
        self.assertIn("No active work", theirs)

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


class RemindTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = mock.patch.object(
            remind_module,
            "REMINDERS_FILE",
            os.path.join(self._temp.name, "reminders.json"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.contacts = {
            "carbon-a": {"contact_type": "carbon"},
            "carbon-b": {"contact_type": "carbon"},
        }
        contacts_patcher = mock.patch.object(
            routing_module, "_local_contacts", return_value=self.contacts
        )
        contacts_patcher.start()
        self.addCleanup(contacts_patcher.stop)

    def test_a_relative_reminder_becomes_a_one_shot_cron(self):
        with mock.patch.object(
            remind_module, "_create_glass_cron", return_value="cron-1"
        ) as create:
            result = _run(
                ["remind", "carbon-b", "--in", "2h", "--text", "check on it"],
                MANAGER_ACTOR,
            )

        trigger = create.call_args.args[0]
        self.assertRegex(trigger, r"^\d+ \d+ \d+ \d+ \*$")
        self.assertEqual(create.call_args.args[1], "check on it")
        self.assertEqual(create.call_args.args[2], [{"kind": "carbon", "id": "carbon-b"}])
        self.assertIn("Reminder r-", result)
        self.assertIn("once at", result)

    def test_unsupported_units_are_refused(self):
        for value in ("30s", "2w", "1y", "banana"):
            with self.subTest(value=value):
                with self.assertRaises(CommandError) as exc:
                    _run(
                        ["remind", "carbon-b", "--in", value, "--text", "x"],
                        MANAGER_ACTOR,
                    )
                self.assertIn("m, h, or d", str(exc.exception))

    def test_a_recurring_reminder_keeps_its_cron_and_timezone(self):
        with mock.patch.object(
            remind_module, "_create_glass_cron", return_value="cron-2"
        ) as create:
            _run(
                [
                    "remind", "carbon-b", "--cron", "0 9 * * 1-5",
                    "--tz", "Asia/Dubai", "--text", "standup",
                ],
                MANAGER_ACTOR,
            )

        self.assertEqual(create.call_args.args[0], "0 9 * * 1-5")
        self.assertEqual(create.call_args.args[3], "Asia/Dubai")

    def test_a_reminder_must_say_when(self):
        with self.assertRaises(CommandError) as exc:
            _run(["remind", "carbon-b", "--text", "x"], MANAGER_ACTOR)
        self.assertIn("Say when", str(exc.exception))

    def test_including_someone_replaces_the_cron_because_glass_cannot_patch_targets(self):
        with mock.patch.object(
            remind_module, "_create_glass_cron", side_effect=["cron-1", "cron-2"]
        ):
            with mock.patch.object(remind_module, "_delete_glass_cron") as delete:
                created = _run(
                    ["remind", "carbon-b", "--cron", "0 9 * * *", "--text", "x"],
                    MANAGER_ACTOR,
                )
                reminder_id = created.split()[1]
                result = _run(
                    ["remind", "--id", reminder_id, "--include", "carbon-a"],
                    MANAGER_ACTOR,
                )

        delete.assert_called_once_with("cron-1")
        self.assertIn("Added carbon:carbon-a", result)
        entry = remind_module._reminders()[reminder_id]
        self.assertEqual(entry["cron_id"], "cron-2")
        self.assertEqual(len(entry["targets"]), 2)

    def test_removing_the_last_target_deletes_the_reminder(self):
        with (
            mock.patch.object(
                remind_module, "_create_glass_cron", return_value="cron-1"
            ),
            mock.patch.object(remind_module, "_delete_glass_cron"),
        ):
            created = _run(
                ["remind", "carbon-b", "--cron", "0 9 * * *", "--text", "x"],
                MANAGER_ACTOR,
            )
            reminder_id = created.split()[1]
            result = _run(
                ["remind", "--id", reminder_id, "--exclude", "carbon-b"],
                MANAGER_ACTOR,
            )

        self.assertIn("Deleted reminder", result)
        self.assertNotIn(reminder_id, remind_module._reminders())

    def test_a_fired_one_shot_is_reaped_and_a_recurring_one_is_not(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        remind_module._store(
            "r-past",
            {"cron_id": "c1", "one_shot": True, "fire_at": past.timestamp()},
        )
        remind_module._store(
            "r-future",
            {"cron_id": "c2", "one_shot": True, "fire_at": future.timestamp()},
        )
        remind_module._store(
            "r-recurring",
            {"cron_id": "c3", "one_shot": False, "trigger": "0 9 * * *"},
        )

        with mock.patch.object(remind_module, "_delete_glass_cron") as delete:
            reaped = remind_module.reap_fired_reminders()

        self.assertEqual(reaped, 1)
        delete.assert_called_once_with("c1")
        self.assertEqual(
            sorted(remind_module._reminders()), ["r-future", "r-recurring"]
        )

    def test_listing_shows_only_that_contacts_reminders(self):
        with mock.patch.object(
            remind_module, "_create_glass_cron", return_value="cron-1"
        ):
            _run(
                ["remind", "carbon-b", "--cron", "0 9 * * *", "--text", "theirs"],
                MANAGER_ACTOR,
            )

        listed = _run(["remind", "carbon-b", "--list"], MANAGER_ACTOR)
        empty = _run(["remind", "carbon-a", "--list"], MANAGER_ACTOR)

        self.assertIn("theirs", listed)
        self.assertIn("No reminders set", empty)


if __name__ == "__main__":
    unittest.main()
