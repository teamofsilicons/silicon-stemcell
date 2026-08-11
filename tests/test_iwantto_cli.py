"""How `iwantto` decides what a command means.

The behaviour that matters most is routing: a manager talking to its own
contact sends straight into the Interface DM, and everyone else is reached
through their manager. Getting that wrong means two managers pestering the same
carbon, or a private message going somewhere it should not.
"""
import io
import os
import contextlib
import tempfile
import unittest
from unittest import mock

from diagnostics.iwantto import actor as actor_module
from diagnostics.iwantto import journal as journal_module
from diagnostics.iwantto import mailbox as mailbox_module
from diagnostics.iwantto import message_log as message_log_module
from diagnostics.iwantto import routing as routing_module
from diagnostics.iwantto.actor import MANAGER, WORKER, Actor
from diagnostics.iwantto.cli import CommandError, build_parser, main as cli_main
from diagnostics.iwantto.commands import messaging


def _args(argv):
    return build_parser().parse_args(argv)


def _run(argv, actor):
    args = _args(argv)
    return args._handler(args, actor)


class _IsolatedState(unittest.TestCase):
    """Point every state file this package writes at a temp directory."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = self._temp.name
        for module, attribute, name in (
            (actor_module, "ACTORS_FILE", "actors.json"),
            (journal_module, "RUNS_FILE", "runs.json"),
            (journal_module, "DIAGNOSIS_DIR", "diagnosis"),
            (mailbox_module, "MAILBOX_FILE", "mailbox.json"),
            (message_log_module, "MESSAGES_DIR", "messages"),
            (routing_module, "ROUTING_FILE", "routing.json"),
        ):
            patcher = mock.patch.object(
                module, attribute, os.path.join(root, name)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        self.manager = Actor(
            kind=MANAGER,
            actor_id="carbon-a",
            contact_id="carbon-a",
            token="token-a",
        )
        self.worker = Actor(
            kind=WORKER,
            actor_id="researcher",
            contact_id="carbon-a",
            worker_type="browser",
            token="token-w",
        )


class TargetResolutionTest(_IsolatedState):
    def test_a_known_contact_resolves_to_its_recorded_type(self):
        contacts = {
            "carbon-b": {"contact_type": "carbon", "display_name": "Bee"},
            "silicon-c": {"contact_type": "silicon", "display_name": "Cee"},
        }
        with mock.patch.object(routing_module, "_local_contacts", return_value=contacts):
            self.assertEqual(routing_module.resolve_target("carbon-b").kind, "carbon")
            self.assertEqual(routing_module.resolve_target("silicon-c").kind, "silicon")
            # And by display name, which is how a Silicon usually refers to them.
            self.assertEqual(
                routing_module.resolve_target("Bee").fixed_id, "carbon-b"
            )

    def test_an_unknown_name_resolves_through_the_glass_trust_directory(self):
        directory = [{"kind": "silicon", "id": "peer-si", "name": "Peer"}]
        with (
            mock.patch.object(routing_module, "_local_contacts", return_value={}),
            mock.patch.object(routing_module, "_trust_directory", return_value=directory),
        ):
            target = routing_module.resolve_target("Peer")

        self.assertEqual(target.kind, "silicon")
        self.assertEqual(target.fixed_id, "peer-si")
        self.assertFalse(target.known)

    def test_a_name_nobody_knows_is_refused_with_a_way_forward(self):
        with (
            mock.patch.object(routing_module, "_local_contacts", return_value={}),
            mock.patch.object(routing_module, "_trust_directory", return_value=[]),
        ):
            with self.assertRaises(routing_module.RoutingError) as refused:
                routing_module.resolve_target("nobody")
            # An explicit type settles it instead of guessing.
            typed = routing_module.resolve_target("nobody", kind_hint="carbon")

        self.assertIn("--carbon nobody", str(refused.exception))
        self.assertEqual(typed.kind, "carbon")

    def test_an_ambiguous_name_is_refused_rather_than_picked(self):
        contacts = {
            "carbon-b": {"contact_type": "carbon", "display_name": "Sam"},
            "carbon-c": {"contact_type": "carbon", "display_name": "Sam"},
        }
        with mock.patch.object(routing_module, "_local_contacts", return_value=contacts):
            with self.assertRaises(routing_module.RoutingError) as exc:
                routing_module.resolve_target("Sam")

        self.assertIn("more than one contact", str(exc.exception))


class SendRoutingTest(_IsolatedState):
    def _contacts(self):
        return {
            "carbon-a": {"contact_type": "carbon", "display_name": "Ay"},
            "carbon-b": {
                "contact_type": "carbon",
                "display_name": "Bee",
                "last_processed_event_id": "event-1",
            },
        }

    def test_a_manager_reaches_its_own_contact_directly(self):
        with (
            mock.patch.object(routing_module, "_local_contacts", return_value=self._contacts()),
            mock.patch("interface.reply_contact", return_value="Message sent") as reply,
            mock.patch("interface.messages.send_manager_message") as via_manager,
        ):
            result = _run(["send", "carbon-a", "--text", "hey"], self.manager)

        reply.assert_called_once()
        self.assertEqual(reply.call_args.args[0], "hey")
        self.assertEqual(reply.call_args.args[1], "carbon-a")
        via_manager.assert_not_called()
        self.assertIn("Sent to", result)

    def test_anyone_else_is_reached_through_their_manager(self):
        with (
            mock.patch.object(routing_module, "_local_contacts", return_value=self._contacts()),
            mock.patch("interface.ensure_contact_for_target", return_value={}),
            mock.patch("interface.reply_contact") as reply,
            mock.patch(
                "interface.messages.send_manager_message", return_value="Done. queued"
            ) as via_manager,
        ):
            result = _run(["send", "carbon-b", "--text", "can you help?"], self.manager)

        reply.assert_not_called()
        via_manager.assert_called_once_with(
            "carbon-a",
            "carbon-b",
            "can you help?",
            target_type="carbon",
            # A manager speaks as itself; only workers need an explicit label.
            sender_label="",
        )
        self.assertIn("manager", result)

    def test_a_first_message_tells_a_new_manager_why_it_exists(self):
        contacts = {"carbon-a": {"contact_type": "carbon"}}
        directory = [{"kind": "carbon", "id": "carbon-new", "name": "New"}]
        with (
            mock.patch.object(routing_module, "_local_contacts", return_value=contacts),
            mock.patch.object(routing_module, "_trust_directory", return_value=directory),
            mock.patch("interface.ensure_contact_for_target", return_value={}),
            mock.patch.object(messaging, "_own_label", return_value="my-silicon"),
            mock.patch(
                "interface.messages.send_manager_message", return_value="Done. queued"
            ) as via_manager,
        ):
            _run(["send", "carbon-new", "--text", "hello"], self.manager)
            first_body = via_manager.call_args.args[2]
            # A second send must not repeat the explanation.
            _run(["send", "carbon-new", "--text", "again"], self.manager)
            second_body = via_manager.call_args.args[2]

        self.assertIn("you are not yet talking to your carbon", first_body)
        self.assertIn("my-silicon", first_body)
        self.assertIn("its advised to pass it forward", first_body)
        self.assertIn("hello", first_body)
        self.assertEqual(second_body, "again")

    def test_direct_delivery_belongs_to_the_managing_actor_alone(self):
        """`send` goes direct only when run by that contact's own manager.

        Every other case routes through the target's manager, so a contact only
        ever hears one voice. A worker shares its manager's contact id, so
        identity has to be checked as well as the target — otherwise a worker
        could message the Carbon behind its own manager's back.
        """
        contacts = {
            "carbon-a": {"contact_type": "carbon", "last_processed_event_id": "e1"},
            "carbon-b": {"contact_type": "carbon", "last_processed_event_id": "e1"},
            "silicon-x": {"contact_type": "silicon", "last_processed_event_id": "e1"},
        }
        advisor = Actor(kind="advisor", actor_id="carbon-a", contact_id="carbon-a")
        silicon_manager = Actor(
            kind=MANAGER, actor_id="silicon-x", contact_id="silicon-x"
        )
        expected = {
            # (actor, target): goes direct?
            (self.manager, "carbon-a"): True,
            (self.manager, "carbon-b"): False,
            (self.manager, "silicon-x"): False,
            # An advisor shares the manager's "I", so it shares this too.
            (advisor, "carbon-a"): True,
            (advisor, "carbon-b"): False,
            # A worker is never the manager, not even of its own contact.
            (self.worker, "carbon-a"): False,
            (self.worker, "carbon-b"): False,
            (self.worker, "silicon-x"): False,
            # A silicon contact's manager talks to that silicon directly.
            (silicon_manager, "silicon-x"): True,
            (silicon_manager, "carbon-a"): False,
        }

        for (actor, target), should_be_direct in expected.items():
            with self.subTest(actor=actor.kind, id=actor.actor_id, target=target):
                with (
                    mock.patch.object(
                        routing_module, "_local_contacts", return_value=contacts
                    ),
                    mock.patch(
                        "interface.ensure_contact_for_target", return_value={}
                    ),
                    mock.patch(
                        "interface.reply_contact", return_value="Message sent"
                    ) as direct,
                    mock.patch(
                        "interface.messages.send_manager_message",
                        return_value="Done. queued",
                    ) as via_manager,
                ):
                    _run(["send", target, "--text", "hi"], actor)

                self.assertEqual(direct.called, should_be_direct)
                self.assertEqual(via_manager.called, not should_be_direct)

    def test_a_routed_worker_message_names_the_worker_not_a_peer_manager(self):
        contacts = {
            "carbon-a": {"contact_type": "carbon", "last_processed_event_id": "e1"},
            "carbon-b": {"contact_type": "carbon", "last_processed_event_id": "e1"},
        }
        with (
            mock.patch.object(routing_module, "_local_contacts", return_value=contacts),
            mock.patch("interface.ensure_contact_for_target", return_value={}),
            mock.patch(
                "interface.messages.send_manager_message", return_value="Done. queued"
            ) as via_manager,
        ):
            _run(["send", "carbon-b", "--text", "a question"], self.worker)

        label = via_manager.call_args.kwargs["sender_label"]
        self.assertIn("browser worker `researcher`", label)
        self.assertIn("carbon-a", label)

    def test_a_worker_reaches_its_manager_by_name(self):
        with (
            mock.patch(
                "interface.messages.send_manager_message", return_value="Done. queued"
            ) as via_manager,
        ):
            result = _run(
                ["send", "manager", "--text", "where do I save this?"], self.worker
            )

        via_manager.assert_called_once()
        self.assertEqual(via_manager.call_args.args[1], "carbon-a")
        self.assertEqual(
            via_manager.call_args.kwargs["sender_label"],
            "browser worker `researcher`",
        )
        self.assertIn("Sent to your manager", result)
        # And it is waiting for the manager on its next command.
        waiting = mailbox_module.drain("manager", "carbon-a")
        self.assertEqual(len(waiting), 1)
        self.assertIn("where do I save this?", waiting[0]["message"])

    def test_a_manager_cannot_send_to_manager(self):
        with self.assertRaises(CommandError) as exc:
            _run(["send", "manager", "--text", "hi"], self.manager)

        self.assertIn("You are the manager", str(exc.exception))

    def test_a_manager_answers_a_worker_without_stopping_it(self):
        record = {"carbon_id": "carbon-a", "state": "active"}
        with mock.patch("worker.handler._get_worker_record", return_value=record):
            result = _run(
                ["send", "researcher", "--text", "save it under /tmp"], self.manager
            )

        self.assertIn("has not been stopped", result)
        waiting = mailbox_module.drain("worker", "researcher")
        self.assertEqual(waiting[0]["message"], "save it under /tmp")

    def test_a_worker_belonging_to_another_manager_is_not_addressable(self):
        record = {"carbon_id": "carbon-z", "state": "active"}
        with (
            mock.patch("worker.handler._get_worker_record", return_value=record),
            mock.patch.object(routing_module, "_local_contacts", return_value={}),
            mock.patch.object(routing_module, "_trust_directory", return_value=[]),
        ):
            with self.assertRaises(CommandError) as exc:
                _run(["send", "researcher", "--text", "hi"], self.manager)

        self.assertIn("I don't know who", str(exc.exception))


class SendBodyTest(_IsolatedState):
    def test_exactly_one_kind_of_body_is_required(self):
        with self.assertRaises(CommandError) as none_given:
            _run(["send", "carbon-a"], self.manager)
        self.assertIn("--text, --file, or --voice", str(none_given.exception))

        with self.assertRaises(CommandError) as both:
            _run(
                ["send", "carbon-a", "--text", "a", "--voice", "b"], self.manager
            )
        self.assertIn("one thing at a time", str(both.exception))

    def test_voice_direction_and_gender_are_composed_into_the_transcript(self):
        args = _args([
            "send", "carbon-a",
            "--voice", "[slow] there are 3 types of people",
            "--voice-direction", "Speak like Gandalf giving a speech.",
            "--voice-gender", "male",
        ])
        message, kind = messaging._build_message(args)

        self.assertEqual(kind, "voice")
        self.assertTrue(message.startswith("[voice="))
        self.assertIn("Voice: male.", message)
        self.assertIn("Speak like Gandalf giving a speech.", message)
        self.assertIn("[slow] there are 3 types of people", message)

    def test_a_missing_file_is_reported_before_anything_is_sent(self):
        with self.assertRaises(CommandError) as exc:
            _run(
                ["send", "carbon-a", "--file", "/nope/missing.pdf"], self.manager
            )
        self.assertIn("No file at", str(exc.exception))

    def test_a_file_is_sent_with_its_caption(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            args = _args([
                "send", "carbon-a", "--file", handle.name, "--caption", "the report",
            ])
            message, kind = messaging._build_message(args)

        self.assertEqual(kind, "file")
        self.assertTrue(message.startswith("the report\n[file="))


class SeeAndBundleTest(_IsolatedState):
    def setUp(self):
        super().setUp()
        message_log_module.record_outbound("carbon-b", "event-1", "first")
        message_log_module.record_inbound("carbon-b", "event-2", "a reply")
        message_log_module.record_outbound("carbon-b", "event-3", "second")
        message_log_module.record_outbound("carbon-b", "event-4", "third")
        self._contacts = {
            "carbon-a": {"contact_type": "carbon"},
            "carbon-b": {
                "contact_type": "carbon",
                "last_processed_event_id": "event-2",
            },
        }

    def test_unread_means_everything_sent_since_their_last_reply(self):
        with mock.patch.object(
            routing_module, "_local_contacts", return_value=self._contacts
        ):
            result = _run(["see", "carbon-b", "--unread"], self.manager)

        self.assertIn("2 message(s)", result)
        self.assertIn("second", result)
        self.assertIn("third", result)
        self.assertNotIn("first", result)

    def test_a_message_can_be_looked_up_by_its_msgid(self):
        result = _run(["see", "--id", "event-2"], self.manager)

        self.assertIn("a reply", result)
        self.assertIn("msgid=event-2", result)

    def test_looking_up_an_unknown_msgid_says_so(self):
        self.assertIn(
            "No message with msgid",
            _run(["see", "--id", "event-999"], self.manager),
        )

    def test_bundling_takes_back_the_unread_pile_and_replaces_it(self):
        with (
            mock.patch.object(
                routing_module, "_local_contacts", return_value=self._contacts
            ),
            mock.patch(
                "interface.take_back_event", return_value="Taken back"
            ) as take_back,
            mock.patch("interface.ensure_contact_for_target", return_value={}),
            mock.patch(
                "interface.messages.send_manager_message", return_value="Done. queued"
            ) as via_manager,
        ):
            result = _run(
                ["bundle-unread", "carbon-b", "--text", "tldr: two things"],
                self.manager,
            )

        self.assertEqual(
            sorted(call.args[0] for call in take_back.call_args_list),
            ["event-3", "event-4"],
        )
        via_manager.assert_called_once()
        self.assertIn("Bundled 2 unread message(s)", result)

    def test_bundling_with_nothing_unread_is_refused(self):
        message_log_module.record_inbound("carbon-b", "event-5", "caught up")
        with mock.patch.object(
            routing_module, "_local_contacts", return_value=self._contacts
        ):
            with self.assertRaises(CommandError) as exc:
                _run(
                    ["bundle-unread", "carbon-b", "--text", "tldr"], self.manager
                )

        self.assertIn("Nothing to bundle", str(exc.exception))


class DispatcherTest(_IsolatedState):
    def test_an_unidentified_caller_exits_without_running_anything(self):
        buffer = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            contextlib.redirect_stderr(buffer),
        ):
            code = cli_main(["send", "carbon-a", "--text", "hi"])

        self.assertEqual(code, 2)
        self.assertIn("could not tell who is running it", buffer.getvalue())

    def test_a_usage_mistake_comes_back_as_an_error_not_a_dead_end(self):
        buffer = io.StringIO()
        with (
            mock.patch(
                "diagnostics.iwantto.cli.resolve_actor", return_value=self.manager
            ),
            contextlib.redirect_stderr(buffer),
        ):
            code = cli_main(["remind", "carbon-b", "--in", "banana", "--text", "x"])

        self.assertEqual(code, 1)
        self.assertIn("--in takes a number then m, h, or d", buffer.getvalue())

    def test_a_successful_command_is_recorded_against_its_run(self):
        with mock.patch(
            "diagnostics.iwantto.cli.resolve_actor", return_value=self.manager
        ):
            code = cli_main(["do-nothing", "--reason", "nothing needs doing"])

        self.assertEqual(code, 0)
        summary = journal_module.run_summary("token-a")
        self.assertTrue(summary.get("did_nothing"))
        self.assertEqual(summary.get("count"), 1)

    def test_doing_nothing_requires_saying_why(self):
        with self.assertRaises(CommandError) as exc:
            _run(["do-nothing"], self.manager)
        self.assertIn("needs a --reason", str(exc.exception))

    def test_pending_mail_is_delivered_on_the_next_command(self):
        mailbox_module.deliver(
            WORKER, "researcher", "the manager of carbon-a", "use /tmp"
        )
        buffer = io.StringIO()
        with (
            mock.patch(
                "diagnostics.iwantto.cli.resolve_actor", return_value=self.worker
            ),
            mock.patch(
                "interface.messages.send_manager_message", return_value="Done. queued"
            ),
            contextlib.redirect_stdout(buffer),
        ):
            code = cli_main(["send", "manager", "--text", "done"])

        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("You have messages", output)
        self.assertIn("use /tmp", output)

    def test_every_invocation_lands_in_the_diagnosis_store(self):
        with mock.patch(
            "diagnostics.iwantto.cli.resolve_actor", return_value=self.manager
        ):
            cli_main(["do-nothing", "--reason", "quiet"])

        entries = journal_module.read_recent()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["command"], "do-nothing")
        self.assertEqual(entries[0]["contact_id"], "carbon-a")
        self.assertTrue(entries[0]["ok"])


class AdviceGuardTest(_IsolatedState):
    def test_only_a_manager_can_ask_for_advice(self):
        with self.assertRaises(CommandError) as worker_asks:
            _run(["get-advice", "what now?"], self.worker)
        self.assertIn("Only a manager", str(worker_asks.exception))

        advisor = Actor(kind="advisor", actor_id="carbon-a", contact_id="carbon-a")
        with self.assertRaises(CommandError) as advisor_asks:
            _run(["get-advice", "what now?"], advisor)
        self.assertIn("You are the advisor", str(advisor_asks.exception))

    def test_advice_is_synchronous_and_returned_to_the_caller(self):
        with mock.patch("manager.advisor.ask", return_value="Delegate it.") as ask:
            result = _run(["get-advice", "should I do this myself?"], self.manager)

        ask.assert_called_once_with("carbon-a", "should I do this myself?")
        self.assertIn("Delegate it.", result)


class DelegateTest(_IsolatedState):
    def test_starting_a_worker_requires_a_checkback(self):
        with self.assertRaises(CommandError) as exc:
            _run(
                [
                    "delegate", "--worker", "browser",
                    "--id", "researcher", "--task", "look it up",
                ],
                self.manager,
            )

        self.assertIn("--checkback-in is required", str(exc.exception))

    def test_a_started_worker_gets_its_checkback_scheduled(self):
        with (
            mock.patch(
                "worker.handler.start_worker", return_value="Done. started"
            ) as start,
            mock.patch("interface.cron.checkback.add_checkback") as add_checkback,
        ):
            result = _run(
                [
                    "delegate", "--worker", "browser",
                    "--id", "researcher", "--task", "look it up",
                    "--checkback-in", "15m",
                ],
                self.manager,
            )

        start.assert_called_once_with(
            "researcher", "look it up", "browser", "carbon-a", incognito=False
        )
        add_checkback.assert_called_once_with("researcher", "carbon-a", 15)
        self.assertIn("Started browser worker", result)

    def test_incognito_is_only_meaningful_for_a_browser(self):
        with self.assertRaises(CommandError) as exc:
            _run(
                [
                    "delegate", "--worker", "terminal", "--id", "t1",
                    "--task", "build", "--checkback-in", "5m", "--incognito",
                ],
                self.manager,
            )

        self.assertIn("only applies to a browser worker", str(exc.exception))

    def test_checkback_rejects_units_that_are_not_minutes(self):
        with self.assertRaises(CommandError) as exc:
            _run(
                [
                    "delegate", "--worker", "writer", "--id", "w1",
                    "--task", "draft", "--checkback-in", "2h",
                ],
                self.manager,
            )

        self.assertIn("Only minutes are supported", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
