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
        interface.CONTACTS_FILE = root / "contacts.json"
        interface.CONTACTS_BACKUP_FILE = root / "contacts_backup.json"
        interface.MEDIA_DIR = root / "media"
        interface.LEGACY_TELEGRAM_CONTACTS_FILE = root / "legacy" / "contacts.json"

    def tearDown(self):
        interface.CONTACTS_FILE = self.old_contacts
        interface.CONTACTS_BACKUP_FILE = self.old_backup
        interface.MEDIA_DIR = self.old_media
        interface.LEGACY_TELEGRAM_CONTACTS_FILE = self.old_legacy
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

    def test_ignored_events_update_local_watermark(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        event = {"type": "m.progress", "event_id": "evt-progress", "room_id": "room-a"}

        processed = interface.process_incoming_event(event, client=object())

        self.assertIsNone(processed)
        contact = interface.get_contact("carbon-a")
        self.assertEqual(contact["last_polled_event_id"], "evt-progress")

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
            self.assertTrue(any(cmd[:4] == [str(exe), "--json", "crons", "update"] and "--active" in cmd for cmd in commands))

    def test_json_parser_uses_last_json_line(self):
        self.assertEqual(interface._parse_json_output("noise\n{\"ok\": true}\n"), {"ok": True})

    def test_remote_browser_url_parser(self):
        self.assertEqual(
            interface.parse_remote_browser_url("Share URL: https://remote.example/session/abc\nexpires soon"),
            "https://remote.example/session/abc",
        )


if __name__ == "__main__":
    unittest.main()
