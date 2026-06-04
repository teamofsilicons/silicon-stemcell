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
        interface.CONTACTS_FILE = root / "contacts.json"
        interface.CONTACTS_BACKUP_FILE = root / "contacts_backup.json"
        interface.MEDIA_DIR = root / "media"

    def tearDown(self):
        interface.CONTACTS_FILE = self.old_contacts
        interface.CONTACTS_BACKUP_FILE = self.old_backup
        interface.MEDIA_DIR = self.old_media
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

    def test_json_parser_uses_last_json_line(self):
        self.assertEqual(interface._parse_json_output("noise\n{\"ok\": true}\n"), {"ok": True})

    def test_remote_browser_url_parser(self):
        self.assertEqual(
            interface.parse_remote_browser_url("Share URL: https://remote.example/session/abc\nexpires soon"),
            "https://remote.example/session/abc",
        )


if __name__ == "__main__":
    unittest.main()
