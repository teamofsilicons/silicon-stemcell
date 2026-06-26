import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import core.interface as interface


class InterfaceStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_contacts = interface.CONTACTS_FILE
        self.old_backup = interface.CONTACTS_BACKUP_FILE
        self.old_media = interface.MEDIA_DIR
        self.old_legacy = interface.LEGACY_TELEGRAM_CONTACTS_FILE
        self.old_boot_event_sync_done = interface._boot_event_sync_done
        interface.CONTACTS_FILE = root / "contacts.json"
        interface.CONTACTS_BACKUP_FILE = root / "contacts_backup.json"
        interface.MEDIA_DIR = root / "media"
        interface.LEGACY_TELEGRAM_CONTACTS_FILE = root / "legacy" / "contacts.json"
        interface._boot_event_sync_done = False
        while True:
            try:
                interface._event_queue.get_nowait()
            except Exception:
                break

    def tearDown(self):
        interface.CONTACTS_FILE = self.old_contacts
        interface.CONTACTS_BACKUP_FILE = self.old_backup
        interface.MEDIA_DIR = self.old_media
        interface.LEGACY_TELEGRAM_CONTACTS_FILE = self.old_legacy
        interface._boot_event_sync_done = self.old_boot_event_sync_done
        self.tmp.cleanup()

    def test_first_carbon_is_central_and_ids_are_fixed(self):
        first, first_new = interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        second, second_new = interface.upsert_contact("carbon", "carbon-b", room_id="room-b")

        self.assertTrue(first_new)
        self.assertTrue(second_new)
        self.assertEqual(first["trust_level"], "ultimate")
        self.assertTrue(first["is_central_carbon"])
        self.assertEqual(second["trust_level"], "very_low")
        self.assertFalse(second["is_central_carbon"])

        state = interface.get_contacts()
        self.assertEqual(state["rooms"]["room-a"], "carbon-a")
        self.assertEqual(state["rooms"]["room-b"], "carbon-b")
        self.assertEqual(state["contacts"]["carbon-a"]["carbon_id"], "carbon-a")

    def test_legacy_contacts_import_preserves_local_trust(self):
        interface.LEGACY_TELEGRAM_CONTACTS_FILE.parent.mkdir(parents=True)
        interface.LEGACY_TELEGRAM_CONTACTS_FILE.write_text(
            '{"contacts":{"old-carbon":{"carbon_id":"old-carbon","contact_type":"carbon","trust_level":"high","is_central_carbon":true,"name":"Old Carbon"}}}',
            encoding="utf-8",
        )

        state = interface.get_contacts()

        self.assertEqual(state["contacts"]["old-carbon"]["trust_level"], "high")
        self.assertTrue(state["contacts"]["old-carbon"]["is_central_carbon"])
        self.assertEqual(state["contacts"]["old-carbon"]["display_name"], "Old Carbon")

    def test_silicon_contact_uses_silicon_id_key(self):
        contact, is_new = interface.upsert_contact("silicon", "si-remote", room_id="room-si")

        self.assertTrue(is_new)
        self.assertEqual(contact["contact_type"], "silicon")
        self.assertEqual(contact["silicon_id"], "si-remote")
        self.assertEqual(interface.get_contacts()["rooms"]["room-si"], "si-remote")

    def test_dm_creation_failure_does_not_create_dead_contact(self):
        class FakeClient:
            def ensure_direct_room(self, contact_type, fixed_id):
                raise RuntimeError("api 404: Target not found.")

        with self.assertRaisesRegex(interface.InterfaceError, "Could not open DM"):
            interface.ensure_contact_for_target("carbon", "missing-carbon", client=FakeClient())

        state = interface.get_contacts()
        self.assertNotIn("missing-carbon", state["contacts"])

    def test_room_discovery_creates_direct_contact_mapping(self):
        class FakeClient:
            def whoami(self):
                return {"carbon_id": "self-carbon"}

            def rooms_list(self):
                return {
                    "rooms": [
                        {
                            "room_id": "room-a",
                            "is_direct": True,
                            "members": [
                                {"contact_type": "carbon", "carbon_id": "self-carbon", "is_self": True},
                                {"contact_type": "carbon", "carbon_id": "carbon-a", "display_name": "Carbon A"},
                            ],
                        }
                    ]
                }

        state = interface.discover_rooms(FakeClient(), force=True)

        self.assertEqual(state["rooms"]["room-a"], "carbon-a")
        self.assertEqual(state["contacts"]["carbon-a"]["display_name"], "Carbon A")
        self.assertEqual(state["contacts"]["carbon-a"]["trust_level"], "ultimate")

    def test_remote_browser_result_uses_posted_branded_url(self):
        posted = {
            "event": {
                "event_id": "evt1",
                "content": {"url": "https://browser.teamofsilicons.com/s/session-123"},
            }
        }

        self.assertEqual(interface._extract_event_id(posted), "evt1")
        self.assertEqual(
            interface._extract_remote_browser_url(posted, fallback="https://api.steel.dev/x"),
            "https://browser.teamofsilicons.com/s/session-123",
        )

    def test_room_discovery_accepts_room_level_contact_fields(self):
        class FakeClient:
            def whoami(self):
                return {"silicon_id": "self-si"}

            def rooms_list(self):
                return {
                    "rooms": [
                        {
                            "id": "room-si",
                            "direct": True,
                            "contact_type": "silicon",
                            "silicon_id": "remote-si",
                            "display_name": "Remote Si",
                        }
                    ]
                }

            def room_members(self, room_id):
                return {"members": []}

        state = interface.discover_rooms(FakeClient(), force=True)

        self.assertEqual(state["rooms"]["room-si"], "remote-si")
        self.assertEqual(state["contacts"]["remote-si"]["contact_type"], "silicon")
        self.assertEqual(state["contacts"]["remote-si"]["display_name"], "Remote Si")

    def test_room_discovery_accepts_direct_room_peers(self):
        class FakeClient:
            def whoami(self):
                return {"silicon_id": "self-si"}

            def rooms_list(self):
                return [
                    {
                        "room_id": "room-carbon",
                        "kind": "direct",
                        "peers": [
                            {
                                "kind": "carbon",
                                "id": "saket",
                                "handle": "saket",
                                "name": "Saket",
                            }
                        ],
                    }
                ]

            def room_members(self, room_id):
                raise AssertionError("peers should be enough for direct room discovery")

        state = interface.discover_rooms(FakeClient(), force=True)

        self.assertEqual(state["rooms"]["room-carbon"], "saket")
        self.assertEqual(state["contacts"]["saket"]["contact_type"], "carbon")
        self.assertEqual(state["contacts"]["saket"]["display_name"], "Saket")

    def test_incoming_event_includes_nested_reply_takeback_and_marks_read(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        calls = []

        class FakeClient:
            def read(self, room_id, event_id):
                calls.append(("read", room_id, event_id))

        event = {
            "type": "m.text",
            "event_id": "evt-1",
            "room_id": "room-a",
            "content": {
                "body": "hello",
                "reply_to_event_id": "evt-0",
                "takeBack": {"requestId": "tb-1"},
            },
        }

        processed = interface.process_incoming_event(event, client=FakeClient())

        self.assertIsNotNone(processed)
        contact_id, context = processed
        self.assertEqual(contact_id, "carbon-a")
        self.assertIn("reply_to: evt-0", context)
        self.assertIn("take_back_request_id: tb-1", context)
        self.assertEqual(calls, [("read", "room-a", "evt-1")])

    def test_polled_events_are_stamped_with_public_room_id(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")

        class FakeClient:
            def events_list(self, room_id, since=""):
                return [
                    {
                        "type": "m.text",
                        "event_id": "evt-1",
                        "room": 10,
                        "content": {"body": "hello"},
                    }
                ]

            def read(self, room_id, event_id):
                pass

        events = interface._poll_room_events(FakeClient(), interface.get_contacts())

        self.assertEqual(events[0]["room_id"], "room-a")
        self.assertEqual(
            interface.process_incoming_event(events[0], client=FakeClient())[0],
            "carbon-a",
        )

    def test_ignored_events_update_local_watermark(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        event = {"type": "m.progress", "event_id": "evt-progress", "room_id": "room-a"}

        processed = interface.process_incoming_event(event, client=object())

        self.assertIsNone(processed)
        contact = interface.get_contact("carbon-a")
        self.assertEqual(contact["last_polled_event_id"], "evt-progress")
        self.assertEqual(interface.get_contacts()["last_event_cursor"], "evt-progress")

    def test_self_sender_handle_updates_watermark_and_drops_echo(self):
        state = interface.get_contacts()
        state["own_ids"] = ["api-dev-test"]
        interface._save_state(state)
        event = {
            "type": "m.text",
            "event_id": "evt-self",
            "room_id": "room-a",
            "sender_handle": "api-dev-test",
            "content": {"body": "my own reply"},
        }

        processed = interface.process_incoming_event(event, client=object())

        self.assertIsNone(processed)
        self.assertIn("evt-self", interface.get_contacts()["processed_events"]["room-a"])
        self.assertEqual(interface.get_contacts()["last_event_cursor"], "evt-self")

    def test_interface_new_command_maps_to_session_command(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")

        class FakeClient:
            def read(self, room_id, event_id):
                pass

        processed = interface.process_incoming_event(
            {"type": "m.text", "event_id": "evt-new", "room_id": "room-a", "content": {"body": "/new"}},
            client=FakeClient(),
        )

        self.assertEqual(processed, ("carbon-a", "[COMMAND: NEW_SESSION]"))
        self.assertEqual(interface.get_contacts()["last_event_cursor"], "evt-new")

    def test_get_unread_events_uses_global_sync_without_room_polling(self):
        class FakeClient:
            def __init__(self):
                self.sync_calls = []
                self.poll_calls = []
                self.read_calls = []

            def whoami(self):
                return {"silicon_id": "self-si"}

            def rooms_list(self):
                return {
                    "rooms": [
                        {
                            "room_id": "room-a",
                            "is_direct": True,
                            "members": [
                                {"contact_type": "silicon", "silicon_id": "self-si", "is_self": True},
                                {"contact_type": "carbon", "carbon_id": "carbon-a", "display_name": "Carbon A"},
                            ],
                        }
                    ]
                }

            def events_sync(self, after="", limit=interface.EVENT_SYNC_LIMIT):
                self.sync_calls.append((after, limit))
                return {
                    "frames": [
                        {
                            "type": "event",
                            "room_id": "room-a",
                            "event": {
                                "type": "m.text",
                                "event_id": "evt-sync",
                                "content": {"body": "hello from sync"},
                            },
                        }
                    ],
                    "next": "evt-sync",
                    "has_more": False,
                }

            def events_list(self, room_id, since=""):
                self.poll_calls.append((room_id, since))
                raise AssertionError("per-room polling should not run")

            def read(self, room_id, event_id):
                self.read_calls.append((room_id, event_id))

        fake = FakeClient()
        with mock.patch.object(interface, "InterfaceClient", return_value=fake), mock.patch.object(interface, "start_listener"):
            contexts = interface.get_unread_events()

        self.assertEqual(fake.sync_calls, [("", interface.EVENT_SYNC_LIMIT)])
        self.assertEqual(fake.poll_calls, [])
        self.assertIn("carbon-a", contexts)
        self.assertIn("hello from sync", contexts["carbon-a"])
        self.assertEqual(interface.get_contacts()["last_event_cursor"], "evt-sync")

    def test_listener_process_uses_one_socket_without_cli_sync(self):
        client = interface.InterfaceClient.__new__(interface.InterfaceClient)
        with mock.patch.object(interface.InterfaceClient, "popen") as popen:
            client.listen_all_process()
        popen.assert_called_once_with(["listen", "all", "--once", "--no-sync"])

    def test_reply_segments_keep_order_and_report_missing_files(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        existing = Path(self.tmp.name) / "file.txt"
        existing.write_text("ok", encoding="utf-8")

        calls = []

        class FakeClient:
            def send(self, room_id, message):
                calls.append(("send", room_id, message))

            def send_file(self, room_id, path):
                calls.append(("send_file", room_id, path))

            def tts(self, room_id, text):
                calls.append(("tts", room_id, text))

        with mock.patch.object(interface, "InterfaceClient", FakeClient):
            result = interface.reply_contact(
                f"one [file={existing}] two [voice=hello [short pause]] three [file=/missing/nope]",
                "carbon-a",
            )

        self.assertIn("Sent with errors", result)
        self.assertEqual(calls[0], ("send", "room-a", "one"))
        self.assertEqual(calls[1][0], "send_file")
        self.assertEqual(calls[2], ("send", "room-a", "two"))
        self.assertEqual(calls[3], ("tts", "room-a", "hello [short pause]"))
        self.assertEqual(calls[4], ("send", "room-a", "three"))


class InterfaceClientTest(unittest.TestCase):
    def test_cli_uses_json_flag_and_parses_json(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "si"
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)

            completed = SimpleNamespace(returncode=0, stdout='{"ok": true}\n', stderr="")
            with mock.patch("subprocess.run", return_value=completed) as run:
                client = interface.InterfaceClient(executable=str(exe), cwd=Path(td))
                payload = client.send("room-1", "hello")

            self.assertEqual(payload, {"ok": True})
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[:3], [str(exe), "--json", "send"])
            self.assertEqual(cmd[3:], ["room-1", "hello"])

    def test_cli_builds_takeback_and_cron_commands(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "si"
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)

            completed = SimpleNamespace(returncode=0, stdout='{"ok": true}\n', stderr="")
            with mock.patch("subprocess.run", return_value=completed) as run:
                client = interface.InterfaceClient(executable=str(exe), cwd=Path(td))
                client.take_back_complete("req-1", "replacement")
                client.take_back_event("evt-1", reason="manual", force=True)
                client.cron_create("0 9 * * *", "task", [{"kind": "carbon", "id": "c"}])
                client.cron_update("cron-1", trigger="0 10 * * *", task="new", active=False)
                client.cron_delete("cron-1")

            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn([str(exe), "--json", "take-back", "complete", "req-1", "replacement"], commands)
            self.assertIn([str(exe), "--json", "take-back", "evt-1", "--reason", "manual", "--force"], commands)
            self.assertIn([str(exe), "--json", "crons", "delete", "cron-1"], commands)
            self.assertTrue(any(cmd[:5] == [str(exe), "--json", "crons", "create", "--trigger"] for cmd in commands))
            # Targets must be repeated `--target kind:id` flags, not a JSON blob.
            self.assertTrue(
                any("--target" in cmd and "carbon:c" in cmd and "--targets" not in cmd for cmd in commands)
            )
            self.assertTrue(any(cmd[:4] == [str(exe), "--json", "crons", "update"] and "--active" in cmd for cmd in commands))

    def test_cli_builds_current_interface_commands(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "si"
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)

            completed = SimpleNamespace(returncode=0, stdout='{"members":[]}\n', stderr="")
            with mock.patch("subprocess.run", return_value=completed) as run:
                client = interface.InterfaceClient(executable=str(exe), cwd=Path(td))
                client.whoami()
                client.room_members("room-1")
                client.ensure_direct_room("carbon", "saket")
                client.events_list("room-1", since="evt-1")
                client.progress("room-1", "manager", "thinking", "running")

            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn([str(exe), "--json", "me"], commands)
            self.assertIn([str(exe), "--json", "rooms", "show", "room-1", "--limit", "0"], commands)
            self.assertIn([str(exe), "--json", "rooms", "direct", "carbon", "saket"], commands)
            self.assertIn([str(exe), "--json", "messages", "list", "room-1", "--limit", "200"], commands)
            self.assertIn([str(exe), "--json", "progress", "room-1", "thinking", "--group", "manager", "--note", "running"], commands)

    def test_json_parser_uses_last_json_line(self):
        self.assertEqual(interface._parse_json_output("noise\n{\"ok\": true}\n"), {"ok": True})

    def test_remote_browser_url_parser(self):
        self.assertEqual(
            interface.parse_remote_browser_url("Share URL: https://remote.example/session/abc\nexpires soon"),
            "https://remote.example/session/abc",
        )



class GlassProfileSyncTest(InterfaceStateTest):
    def test_glass_central_carbon_overrides_local_first_contact(self):
        # A lord DMs first — the local bootstrap wrongly flags them central.
        interface.upsert_contact("carbon", "lord-1", room_id="room-lord")
        interface.upsert_contact("carbon", "alice", room_id="room-alice")
        self.assertTrue(interface.get_contact("lord-1")["is_central_carbon"])

        # Glass says: still unclaimed (the lord never claims) → flag withdrawn.
        interface._sync_profile_from_glass({"silicon_id": "self", "central_carbon": None})
        self.assertFalse(interface.get_contact("lord-1")["is_central_carbon"])
        self.assertEqual(interface.get_central_contact_id(), "")

        # Alice sends the first real message — Glass reports the claim.
        interface._sync_profile_from_glass(
            {"silicon_id": "self", "central_carbon": {"carbon_id": "alice", "username": "alice", "name": "Alice"}}
        )
        alice = interface.get_contact("alice")
        self.assertTrue(alice["is_central_carbon"])
        self.assertEqual(alice["trust_level"], "ultimate")
        self.assertFalse(interface.get_contact("lord-1")["is_central_carbon"])
        # The lord's locally granted trust is preserved — only the flag moves.
        self.assertEqual(interface.get_contact("lord-1")["trust_level"], "ultimate")

    def test_absent_central_carbon_key_keeps_local_state(self):
        interface.upsert_contact("carbon", "alice", room_id="room-a")
        interface._sync_profile_from_glass({"silicon_id": "self", "name": "Ada Silicon"})
        self.assertTrue(interface.get_contact("alice")["is_central_carbon"])

    def test_profile_caches_description_for_prompts(self):
        interface._sync_profile_from_glass(
            {
                "silicon_id": "self",
                "name": "Ada Silicon",
                "tagline": "designs systems",
                "description": "Handles inbound sales emails.",
                "central_carbon": None,
            }
        )
        profile = interface.get_own_profile()
        self.assertEqual(profile["description"], "Handles inbound sales emails.")
        self.assertEqual(profile["central_carbon"], None)

    def test_discover_rooms_reconciles_from_whoami(self):
        class FakeClient:
            def whoami(self):
                return {
                    "silicon_id": "self-si",
                    "description": "Sales silicon.",
                    "central_carbon": {"carbon_id": "carbon-b", "username": "bee", "name": "Bee"},
                }

            def rooms_list(self):
                return {
                    "rooms": [
                        {
                            "room_id": "room-a",
                            "is_direct": True,
                            "members": [
                                {"contact_type": "silicon", "silicon_id": "self-si", "is_self": True},
                                {"contact_type": "carbon", "carbon_id": "carbon-a"},
                            ],
                        },
                        {
                            "room_id": "room-b",
                            "is_direct": True,
                            "members": [
                                {"contact_type": "silicon", "silicon_id": "self-si", "is_self": True},
                                {"contact_type": "carbon", "carbon_id": "carbon-b"},
                            ],
                        },
                    ]
                }

        state = interface.discover_rooms(FakeClient(), force=True)
        # carbon-a was discovered first (local bootstrap), but Glass says carbon-b.
        self.assertFalse(state["contacts"]["carbon-a"]["is_central_carbon"])
        self.assertTrue(state["contacts"]["carbon-b"]["is_central_carbon"])
        self.assertEqual(state["contacts"]["carbon-b"]["trust_level"], "ultimate")
        self.assertEqual(state["profile"]["description"], "Sales silicon.")


if __name__ == "__main__":
    unittest.main()
