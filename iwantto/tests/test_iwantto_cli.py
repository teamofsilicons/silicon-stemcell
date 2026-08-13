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

from helpers.session import SILICON
from iwantto import actor as actor_module
from diagnostics import journal as journal_module
from iwantto import mailbox as mailbox_module
from iwantto import message_log as message_log_module
from iwantto import routing as routing_module
from iwantto.actor import MANAGER, WORKER, Actor
from iwantto.cli import CommandError, build_parser, main as cli_main
from iwantto.commands import messaging


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
            "silicon-x": {"contact_type": "silicon", "display_name": "Ex"},
        }

    def test_everyone_is_reached_in_one_hop(self):
        """The id you type is the chat it lands in.

        There used to be a manager per contact, so anyone but your own carbon was
        reached by handing the message to *their* manager. That is the hop this
        change exists to delete: one session, one send.
        """
        for target in ("carbon-a", "carbon-b", "silicon-x"):
            with self.subTest(target=target):
                with (
                    mock.patch.object(
                        routing_module, "_local_contacts", return_value=self._contacts()
                    ),
                    mock.patch("interface.ensure_contact_for_target", return_value={}),
                    mock.patch(
                        "interface.reply_contact", return_value="Message sent"
                    ) as reply,
                    mock.patch(
                        "interface.messages.send_manager_message"
                    ) as via_manager,
                ):
                    result = _run(["send", target, "--text", "hey"], self.manager)

                self.assertEqual(reply.call_args.args, ("hey", target))
                via_manager.assert_not_called()
                self.assertIn("Sent to", result)

    def test_an_advisor_shares_the_sessions_voice(self):
        advisor = Actor(kind="advisor", actor_id=SILICON, contact_id=SILICON)
        with (
            mock.patch.object(
                routing_module, "_local_contacts", return_value=self._contacts()
            ),
            mock.patch("interface.ensure_contact_for_target", return_value={}),
            mock.patch(
                "interface.reply_contact", return_value="Message sent"
            ) as reply,
        ):
            _run(["send", "carbon-b", "--text", "hi"], advisor)

        self.assertEqual(reply.call_args.args, ("hi", "carbon-b"))

    def test_a_worker_cannot_talk_to_a_contact_behind_the_sessions_back(self):
        with (
            mock.patch.object(
                routing_module, "_local_contacts", return_value=self._contacts()
            ),
            mock.patch("interface.ensure_contact_for_target", return_value={}),
            mock.patch("interface.reply_contact") as reply,
            mock.patch("interface.messages.send_manager_message") as via_manager,
            self.assertRaises(CommandError) as refused,
        ):
            _run(["send", "carbon-b", "--text", "a question"], self.worker)

        reply.assert_not_called()
        via_manager.assert_not_called()
        self.assertIn("Send it to `manager`", str(refused.exception))

    def test_an_unreachable_target_is_refused_before_anything_is_sent(self):
        with (
            mock.patch.object(
                routing_module, "_local_contacts", return_value=self._contacts()
            ),
            mock.patch(
                "interface.ensure_contact_for_target",
                side_effect=Exception("api 404: no such contact"),
            ),
            mock.patch("interface.reply_contact") as reply,
            self.assertRaises(CommandError) as refused,
        ):
            _run(["send", "carbon-b", "--text", "hi"], self.manager)

        reply.assert_not_called()
        self.assertIn("Could not reach", str(refused.exception))

    def test_a_send_never_closes_a_work(self):
        """`--final` is gone. Talking and finishing are separate acts now.

        A message that happens to sound conclusive must not quietly settle a
        durable card — `iwantto work --completed` is the only thing that does.
        """
        lifecycle = mock.Mock()
        with (
            mock.patch.object(
                routing_module, "_local_contacts", return_value=self._contacts()
            ),
            mock.patch("interface.ensure_contact_for_target", return_value={}),
            mock.patch("interface.reply_contact", return_value="Message sent"),
            mock.patch(
                "interface.long_tasks.registry.current_long_task",
                return_value=lifecycle,
            ),
        ):
            _run(["send", "carbon-b", "--text", "all done"], self.manager)
            with self.assertRaises(CommandError):
                _run(["send", "carbon-b", "--text", "all done", "--final"], self.manager)

        lifecycle.deliver_final_reply.assert_not_called()


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

    def test_bundling_takes_back_a_named_range_and_replaces_it(self):
        with (
            mock.patch.object(
                routing_module, "_local_contacts", return_value=self._contacts
            ),
            mock.patch(
                "interface.take_back_event", return_value="Taken back"
            ) as take_back,
            mock.patch("interface.ensure_contact_for_target", return_value={}),
            mock.patch(
                "interface.reply_contact", return_value="Message sent"
            ) as replaced_with,
        ):
            result = _run(
                [
                    "bundle", "carbon-b", "--from", "event-3", "--to", "event-4",
                    "--text", "tldr: two things",
                ],
                self.manager,
            )

        self.assertEqual(
            sorted(call.args[0] for call in take_back.call_args_list),
            ["event-3", "event-4"],
        )
        self.assertEqual(
            replaced_with.call_args.args, ("tldr: two things", "carbon-b")
        )
        self.assertIn("Bundled 2 of my 2 message(s)", result)

    def test_bundling_works_on_messages_they_have_already_seen(self):
        """The whole reason the range is named rather than inferred.

        "Unanswered" could not find a pile a carbon has read and ignored, which
        is exactly the pile worth collapsing.
        """
        message_log_module.record_inbound("carbon-b", "event-5", "caught up")
        with (
            mock.patch.object(
                routing_module, "_local_contacts", return_value=self._contacts
            ),
            mock.patch(
                "interface.take_back_event", return_value="Taken back"
            ) as take_back,
            mock.patch("interface.ensure_contact_for_target", return_value={}),
            mock.patch("interface.reply_contact", return_value="Message sent"),
        ):
            result = _run(
                ["bundle", "carbon-b", "--from", "event-1", "--text", "tldr"],
                self.manager,
            )

        # event-1, event-3, event-4 are mine; event-2 and event-5 are theirs.
        self.assertEqual(
            sorted(call.args[0] for call in take_back.call_args_list),
            ["event-1", "event-3", "event-4"],
        )
        self.assertIn("Left 2 of their message(s) in place", result)

    def test_bundling_needs_a_range_and_an_msgid_it_recognises(self):
        with mock.patch.object(
            routing_module, "_local_contacts", return_value=self._contacts
        ):
            with self.assertRaises(CommandError) as missing:
                _run(["bundle", "carbon-b", "--text", "tldr"], self.manager)
            with self.assertRaises(CommandError) as unknown:
                _run(
                    ["bundle", "carbon-b", "--from", "nope", "--text", "tldr"],
                    self.manager,
                )

        self.assertIn("Which messages?", str(missing.exception))
        self.assertIn("not a message I have", str(unknown.exception))

    def test_bundling_refuses_to_withdraw_only_their_words(self):
        with (
            mock.patch.object(
                routing_module, "_local_contacts", return_value=self._contacts
            ),
            mock.patch("interface.take_back_event") as take_back,
        ):
            with self.assertRaises(CommandError) as exc:
                _run(
                    [
                        "bundle", "carbon-b", "--from", "event-2", "--to", "event-2",
                        "--text", "tldr",
                    ],
                    self.manager,
                )

        take_back.assert_not_called()
        self.assertIn("not what they did", str(exc.exception))


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
                "iwantto.cli.resolve_actor", return_value=self.manager
            ),
            contextlib.redirect_stderr(buffer),
        ):
            code = cli_main(["remember", "--in", "banana", "--text", "x"])

        self.assertEqual(code, 1)
        self.assertIn("--in takes a number then m, h, or d", buffer.getvalue())

    def test_a_successful_command_is_recorded_against_its_run(self):
        with mock.patch(
            "iwantto.cli.resolve_actor", return_value=self.manager
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
                "iwantto.cli.resolve_actor", return_value=self.worker
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
            "iwantto.cli.resolve_actor", return_value=self.manager
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
        with mock.patch("silicon.advisor.ask", return_value="Delegate it.") as ask:
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
                "worker.start_worker", return_value="Done. started"
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
