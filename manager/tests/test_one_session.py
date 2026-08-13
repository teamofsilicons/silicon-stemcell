"""One session for everybody, and each chat still its own chat.

There used to be a manager per contact, and one more per Silicon it could talk
to, so "ask silicon B for X" crossed four sessions before anyone started on X.
These are the properties that replaced those hops, and the one they must not
cost: a frame the Silicon did not address to anybody still has to find the right
room.
"""
import unittest
from unittest import mock

from helpers import silicon
from helpers.silicon import SILICON, answering, envelope, origins_in
from interface import outbound as i_outbound
import manager.dispatcher as m_manager_dispatcher
import manager.loop as m_manager_loop


class OneSessionTest(unittest.TestCase):
    def test_every_handler_merges_into_the_one_session(self):
        """Two people and a cron in one tick are one root, not three turns."""
        handlers = [
            {"name": "messages", "execute": lambda: {
                "carbon-a": "from a", "carbon-b": "from b",
            }},
            {"name": "crons", "execute": lambda: {"carbon-a": "cron fired"}},
        ]
        with mock.patch.object(m_manager_loop, "EVENT_LOOP", handlers):
            merged = m_manager_loop.run_event_loop_tick()

        self.assertEqual(list(merged), [SILICON])
        for part in ("from a", "from b", "cron fired"):
            self.assertIn(part, merged[SILICON])

    def test_a_quiet_tick_produces_no_root(self):
        handlers = [{"name": "messages", "execute": lambda: {}}]
        with mock.patch.object(m_manager_loop, "EVENT_LOOP", handlers):
            self.assertEqual(m_manager_loop.run_event_loop_tick(), {})

    def test_a_handler_that_throws_does_not_lose_the_others(self):
        def boom():
            raise RuntimeError("glass unreachable")

        handlers = [
            {"name": "broken", "execute": boom},
            {"name": "messages", "execute": lambda: {"carbon-a": "still here"}},
        ]
        with mock.patch.object(m_manager_loop, "EVENT_LOOP", handlers):
            merged = m_manager_loop.run_event_loop_tick()

        self.assertEqual(merged[SILICON], "still here")


class TurnOriginTest(unittest.TestCase):
    """Who a turn is answering, recovered from the text of its own root.

    The origin has to survive a restart, which is why it travels in the context
    rather than in memory beside it.
    """

    def test_a_batch_of_two_is_answering_both(self):
        context = "\n\n".join([
            envelope("carbon-a", display_name="Ay", trust="ok") + " deploy?",
            envelope("silicon-b", contact_type="silicon", trust="high") + " need X",
        ])
        self.assertEqual(origins_in(context), ["carbon-a", "silicon-b"])

    def test_an_untargeted_frame_reaches_every_room_in_the_turn(self):
        """A spinner belongs to whoever is waiting, and only to them."""
        sent = []
        contacts = {
            "carbon-a": {"room_id": "room-a"},
            "silicon-b": {"room_id": "room-b"},
        }
        with (
            mock.patch.object(
                i_outbound, "get_contact", side_effect=contacts.get
            ),
            mock.patch.object(
                i_outbound.client_module, "InterfaceClient"
            ) as client,
            mock.patch("interface.work.current_manager_activity_group",
                       return_value="group-1"),
            mock.patch("interface.work.touch_manager_call_activity"),
            mock.patch("interface.work.activity_frame_identity",
                       return_value=("frame-1", 3, None)),
            mock.patch("interface.work.canonical_activity_state",
                       side_effect=lambda state: state),
            mock.patch(
                "helpers.process.submit_best_effort",
                side_effect=lambda fn, *a, **k: sent.append(a),
            ),
        ):
            client.return_value.progress = mock.Mock()
            with answering(["carbon-a", "silicon-b"]):
                i_outbound.send_progress(SILICON, "group-1", "thinking", "working")

        # (contact_id, room_id, group, state, message, frame_id, task_id, revision, ...)
        self.assertEqual([call[1] for call in sent], ["room-a", "room-b"])
        # One activity, not one per room: the frame's identity and revision are
        # resolved once, so a fanned-out spinner cannot diverge between chats.
        self.assertEqual({call[5] for call in sent}, {"frame-1"})
        self.assertEqual({call[7] for call in sent}, {3})

    def test_a_named_target_never_fans_out(self):
        with answering(["carbon-a", "silicon-b"]):
            self.assertEqual(silicon.resolve_rooms("carbon-a"), ("carbon-a",))

    def test_a_frame_with_nobody_waiting_is_refused_not_broadcast(self):
        """Outside a turn there is no room, and guessing one would be worse."""
        self.assertEqual(silicon.live_origins(), ())
        status = i_outbound.reply_contact("anyone there?", SILICON)
        self.assertTrue(status.startswith("Error"))
        self.assertIn("answering no message", status)

    def test_a_message_arriving_mid_turn_joins_what_it_is_answering(self):
        """The whole point of injection: it lands now, and it is heard from.

        Its sender has to become part of the live turn too, or the reply it
        earns would have nowhere to go.
        """
        dispatcher = m_manager_dispatcher.ManagerDispatcher(runner=lambda _: None)
        admission = mock.Mock()
        admission.contact_id = SILICON
        admission.context = envelope("carbon-late", trust="ok") + " one more thing"

        with answering(["carbon-a"]):
            with mock.patch.object(
                m_manager_dispatcher.injection, "offer", return_value=True
            ):
                self.assertTrue(dispatcher._inject_into_live_run(admission))
            self.assertEqual(
                silicon.live_origins(), ("carbon-a", "carbon-late")
            )

        # And it leaves with the turn that absorbed it.
        self.assertEqual(silicon.live_origins(), ())


class CommandTest(unittest.TestCase):
    """`/new` and `/start` still answer the person who typed them."""

    def test_a_command_answers_the_contact_who_typed_it(self):
        """The reply goes to them by name, not to whoever the turn is answering.

        A command carries no envelope, so there is no origin to fan out to — the
        marker names its sender for exactly this reason.
        """
        with (
            mock.patch.object(m_manager_loop, "new_session", return_value="s-1"),
            mock.patch.object(
                m_manager_loop.outbound, "reply_contact", return_value="Message sent"
            ) as reply,
            mock.patch.object(
                m_manager_loop.Diagnostics, "get_active_run", return_value=None
            ),
        ):
            cleaned = m_manager_loop.handle_commands(
                {SILICON: "[COMMAND: NEW_SESSION from carbon-a]"}
            )

        self.assertEqual(cleaned, {})
        self.assertEqual(reply.call_args.args[1], "carbon-a")

    def test_a_command_alongside_a_message_keeps_the_message(self):
        message = envelope("carbon-b", trust="ok") + " and here is a question"
        with (
            mock.patch.object(m_manager_loop, "new_session", return_value="s-1"),
            mock.patch.object(
                m_manager_loop.outbound, "reply_contact", return_value="Message sent"
            ),
            mock.patch.object(
                m_manager_loop.Diagnostics, "get_active_run", return_value=None
            ),
        ):
            cleaned = m_manager_loop.handle_commands({
                SILICON: f"[COMMAND: START from carbon-a]\n\n{message}",
            })

        self.assertEqual(cleaned[SILICON], message)
        self.assertEqual(origins_in(cleaned[SILICON]), ["carbon-b"])


if __name__ == "__main__":
    unittest.main()
