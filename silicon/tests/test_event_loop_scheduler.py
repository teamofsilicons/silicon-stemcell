import unittest
from unittest import mock

import silicon.loop_config as m_manager_loop_config
import silicon.loop as m_manager_loop


class EventLoopScheduleTest(unittest.TestCase):
    def setUp(self):
        self.handlers = [
            {
                "name": "interactive",
                "execute": lambda: None,
                "interval_seconds": 60,
                "jitter_seconds": 0,
                "run_on_activity": True,
                "run_on_startup": True,
            },
            {
                "name": "hourly",
                "execute": lambda: None,
                "interval_seconds": 3600,
                "jitter_seconds": 300,
            },
        ]

    def test_startup_and_activity_do_not_reset_independent_deadlines(self):
        schedule = m_manager_loop.EventLoopSchedule(
            self.handlers,
            now=100.0,
            identity="silicon-a",
        )
        self.assertEqual(schedule.due(100.0), {"interactive"})
        schedule.record_attempts({"interactive"}, 100.0)
        interactive_deadline = schedule.next_due["interactive"]

        self.assertEqual(
            schedule.due(110.0, activity=True),
            {"interactive"},
        )
        schedule.record_attempts({"interactive"}, 110.0)
        self.assertEqual(schedule.next_due["interactive"], interactive_deadline)

    def test_slow_handlers_are_jittered_deterministically(self):
        first = m_manager_loop.EventLoopSchedule(
            self.handlers,
            now=0.0,
            identity="silicon-a",
        )
        second = m_manager_loop.EventLoopSchedule(
            self.handlers,
            now=0.0,
            identity="silicon-a",
        )
        other = m_manager_loop.EventLoopSchedule(
            self.handlers,
            now=0.0,
            identity="silicon-b",
        )
        self.assertEqual(first.next_due["hourly"], second.next_due["hourly"])
        self.assertNotEqual(first.next_due["hourly"], other.next_due["hourly"])
        self.assertGreaterEqual(first.next_due["hourly"], 3600.0)
        self.assertLessEqual(first.next_due["hourly"], 3900.0)

    def test_empty_selection_runs_no_handlers(self):
        with mock.patch.object(m_manager_loop_config, "EVENT_LOOP", self.handlers):
            self.assertEqual(m_manager_loop.run_event_loop_tick(set()), {})


if __name__ == "__main__":
    unittest.main()
