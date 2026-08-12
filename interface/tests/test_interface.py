import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import interface
import interface.client
import interface.contacts
import interface.events
import interface.inbox
import interface.inbox_file
import interface.ingest
import interface.outbound
import interface.remote_browser
import interface.state


class InterfaceStateTest(unittest.TestCase):
    def setUp(self):
        interface.stop_runtime_file_watch()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_contacts = interface.constants.CONTACTS_FILE
        self.old_backup = interface.constants.CONTACTS_BACKUP_FILE
        self.old_media = interface.constants.MEDIA_DIR
        self.old_inbox_consumer = interface.constants.INBOX_CONSUMER_FILE
        self.old_default_inbox = interface.constants.DEFAULT_INBOX_FILE
        interface.constants.CONTACTS_FILE = root / "contacts.json"
        interface.constants.CONTACTS_BACKUP_FILE = root / "contacts_backup.json"
        interface.constants.MEDIA_DIR = root / "media"
        interface.constants.INBOX_CONSUMER_FILE = root / "interface_inbox_consumer.json"
        interface.constants.DEFAULT_INBOX_FILE = root / "inbox.jsonl"
        with interface.inbox_file._inbox_scan_lock:
            interface.inbox_file._inbox_scan_state.clear()
        with interface.inbox._activity_condition:
            interface.inbox._activity_pending = 0
        with interface.inbox._inbox_retry_lock:
            interface.inbox._inbox_retry_records.clear()
        while True:
            try:
                interface.inbox._event_queue.get_nowait()
            except Exception:
                break

    def tearDown(self):
        interface.stop_runtime_file_watch()
        interface.constants.CONTACTS_FILE = self.old_contacts
        interface.constants.CONTACTS_BACKUP_FILE = self.old_backup
        interface.constants.MEDIA_DIR = self.old_media
        interface.constants.INBOX_CONSUMER_FILE = self.old_inbox_consumer
        interface.constants.DEFAULT_INBOX_FILE = self.old_default_inbox
        with interface.inbox_file._inbox_scan_lock:
            interface.inbox_file._inbox_scan_state.clear()
        with interface.inbox._activity_condition:
            interface.inbox._activity_pending = 0
        with interface.inbox._inbox_retry_lock:
            interface.inbox._inbox_retry_records.clear()
        self.tmp.cleanup()

    def test_new_contacts_fail_closed_and_ids_are_fixed(self):
        first, first_new = interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        second, second_new = interface.upsert_contact("carbon", "carbon-b", room_id="room-b")

        self.assertTrue(first_new)
        self.assertTrue(second_new)
        self.assertEqual(first["trust_level"], "very_low")
        self.assertFalse(first["is_central_carbon"])
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
        self.assertEqual(state["contacts"]["carbon-a"]["trust_level"], "very_low")

    def test_room_discovery_identity_update_preserves_concurrent_state(self):
        class FakeClient:
            def whoami(self):
                interface.upsert_contact(
                    "carbon",
                    "arrived-during-whoami",
                    room_id="room-concurrent",
                )
                return {"silicon_id": "self-si"}

            def rooms_list(self):
                return []

        state = interface.discover_rooms(FakeClient(), force=True)

        self.assertEqual(state["own_ids"], ["self-si"])
        self.assertIn("arrived-during-whoami", state["contacts"])
        self.assertEqual(
            state["rooms"]["room-concurrent"],
            "arrived-during-whoami",
        )

    def test_remote_browser_result_uses_posted_branded_url(self):
        posted = {
            "event": {
                "event_id": "evt1",
                "content": {"url": "https://browser.teamofsilicons.com/s/session-123"},
            }
        }

        self.assertEqual(interface.remote_browser._extract_event_id(posted), "evt1")
        self.assertEqual(
            interface.remote_browser._extract_remote_browser_url(posted, fallback="https://api.steel.dev/x"),
            "https://browser.teamofsilicons.com/s/session-123",
        )

    def test_remote_browser_new_opens_page_then_shares_existing_session(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        posted = {
            "event": {
                "event_id": "evt1",
                "content": {"url": "https://browser.teamofsilicons.com/s/session-123"},
            }
        }
        fake_client = mock.Mock()
        fake_client.remote_browser.return_value = posted
        completed = [
            subprocess.CompletedProcess([], 1, stdout="", stderr="no session"),
            subprocess.CompletedProcess([], 0, stdout="opened", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="Share URL: https://remote.example/session/abc", stderr=""),
        ]

        with (
            mock.patch("worker.constants.SILICON_BROWSER_PROFILE", "profile-a"),
            mock.patch("subprocess.run", side_effect=completed) as run,
            mock.patch.object(interface.client, "InterfaceClient", return_value=fake_client),
            mock.patch.object(interface.remote_browser, "_save_remote_browser_event"),
        ):
            result = interface.remote_browser_share("carbon-a", expiry=120, new=True, url="example.com")

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            commands,
            [
                [
                    "silicon-browser",
                    "--session",
                    "remote-carbon-a",
                    "--profile",
                    "profile-a",
                    "close",
                ],
                [
                    "silicon-browser",
                    "--session",
                    "remote-carbon-a",
                    "--profile",
                    "profile-a",
                    "open",
                    "https://example.com",
                    "--timeout",
                    "120",
                ],
                [
                    "silicon-browser",
                    "--session",
                    "remote-carbon-a",
                    "--profile",
                    "profile-a",
                    "share",
                    "--expiry",
                    "120",
                ],
            ],
        )
        self.assertNotIn("--new", commands[-1])
        fake_client.remote_browser.assert_called_once_with(
            "room-a",
            "https://remote.example/session/abc",
            120,
        )
        self.assertIn("https://browser.teamofsilicons.com/s/session-123", result)

    def test_remote_browser_reuses_existing_or_opens_default_if_missing(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        fake_client = mock.Mock()
        fake_client.remote_browser.return_value = {
            "event": {"event_id": "evt1", "content": {"url": "https://browser.teamofsilicons.com/s/session-123"}}
        }
        completed = [
            subprocess.CompletedProcess(
                [],
                1,
                stdout="",
                stderr="No active session named 'remote-carbon-a'. Open a page first.",
            ),
            subprocess.CompletedProcess([], 0, stdout="opened", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="Share URL: https://remote.example/session/abc", stderr=""),
        ]

        with (
            mock.patch("worker.constants.SILICON_BROWSER_PROFILE", "profile-a"),
            mock.patch.object(interface.remote_browser, "REMOTE_BROWSER_START_URL", "https://default.test"),
            mock.patch("subprocess.run", side_effect=completed) as run,
            mock.patch.object(interface.client, "InterfaceClient", return_value=fake_client),
            mock.patch.object(interface.remote_browser, "_save_remote_browser_event"),
        ):
            result = interface.remote_browser_share("carbon-a", expiry=45, new=False)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][-3:], ["share", "--expiry", "45"])
        self.assertEqual(commands[1][-4:], ["open", "https://default.test", "--timeout", "45"])
        self.assertEqual(commands[2][-3:], ["share", "--expiry", "45"])
        for command in commands:
            self.assertNotIn("--new", command)
        self.assertIn("Remote browser shared", result)

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

        def run_immediately(function, *args, **_kwargs):
            function(*args)
            return True

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

        with mock.patch(
            "helpers.process.submit_best_effort",
            side_effect=run_immediately,
        ):
            processed = interface.process_incoming_event(event, client=FakeClient())

        self.assertIsNotNone(processed)
        contact_id, context = processed
        self.assertEqual(contact_id, "carbon-a")
        self.assertIn("reply_to: evt-0", context)
        self.assertIn("take_back_request_id: tb-1", context)
        self.assertEqual(calls, [("read", "room-a", "evt-1")])

    def test_attachment_metadata_lookup_is_reused_for_bookkeeping(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")

        class FakeClient:
            def __init__(self):
                self.media_lookups = 0

            def media_show(self, media_id):
                self.media_lookups += 1
                self.assert_media_id = media_id
                return {
                    "download_url": "https://download.example/media-1",
                    "s3_url": "https://cdn.example/media-1",
                    "filename": "report.pdf",
                }

            def read(self, room_id, event_id):
                pass

        def run_immediately(function, *args, **_kwargs):
            function(*args)
            return True

        client = FakeClient()
        local_path = str(Path(self.tmp.name) / "media" / "evt-media_report.pdf")
        with (
            mock.patch.object(interface.ingest, "_download_url", return_value=local_path),
            mock.patch("helpers.process.submit_best_effort", side_effect=run_immediately),
            mock.patch("diagnostics.activity.incoming") as log_incoming,
            mock.patch("diagnostics.store.Diagnostics.get_active_run", return_value=None),
            mock.patch("diagnostics.store.Diagnostics.start_run", side_effect=RuntimeError),
        ):
            processed = interface.process_incoming_event(
                {
                    "type": "m.file",
                    "event_id": "evt-media",
                    "room_id": "room-a",
                    "content": {
                        "media_id": "media-1",
                        "filename": "report.pdf",
                    },
                },
                client=client,
            )

        self.assertEqual(processed[0], "carbon-a")
        self.assertEqual(client.media_lookups, 1)
        self.assertEqual(client.assert_media_id, "media-1")
        log_incoming.assert_called_once_with(
            "carbon-a",
            "m.file",
            body="",
            media_id="media-1",
            attachment_url="https://cdn.example/media-1",
            event_id="evt-media",
        )

    def test_album_event_downloads_each_unique_item_and_includes_caption(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")

        class FakeClient:
            def __init__(self):
                self.media_lookups = []
                self.reads = []

            def media_show(self, media_id):
                self.media_lookups.append(media_id)
                return {
                    "download_url": f"https://download.example/{media_id}",
                    "filename": f"{media_id}.bin",
                }

            def read(self, room_id, event_id):
                self.reads.append((room_id, event_id))

        def run_immediately(function, *args, **_kwargs):
            function(*args)
            return True

        client = FakeClient()
        event = {
            "type": "m.album",
            "event_id": "evt-album",
            "room_id": "room-a",
            "content": {
                "caption": "Review both company documents.",
                "items": [
                    {"media_id": "media-pdf", "filename": "booklet.pdf"},
                    {"media_id": "media-zip", "filename": "documents.zip"},
                ],
            },
            # The durable inbox also projects album items here. They must not
            # cause duplicate downloads.
            "media_items": [
                {"position": 0, "media_id": "media-pdf", "filename": "booklet.pdf"},
                {"position": 1, "media_id": "media-zip", "filename": "documents.zip"},
            ],
        }

        def downloaded(_url, path):
            return str(path)

        with (
            mock.patch.object(interface.ingest, "_download_url", side_effect=downloaded),
            mock.patch("helpers.process.submit_best_effort", side_effect=run_immediately),
            mock.patch("diagnostics.store.Diagnostics.get_active_run", return_value=None),
            mock.patch("diagnostics.store.Diagnostics.start_run", side_effect=RuntimeError),
        ):
            processed = interface.process_incoming_event(event, client=client)

        self.assertEqual(processed[0], "carbon-a")
        self.assertIn("event_type: m.album", processed[1])
        self.assertIn("message:\nReview both company documents.", processed[1])
        self.assertIn("evt-album_1_booklet.pdf", processed[1])
        self.assertIn("evt-album_2_documents.zip", processed[1])
        self.assertLess(
            processed[1].index("evt-album_1_booklet.pdf"),
            processed[1].index("evt-album_2_documents.zip"),
        )
        self.assertEqual(client.media_lookups, ["media-pdf", "media-zip"])
        self.assertEqual(client.reads, [("room-a", "evt-album")])

    def test_album_without_caption_still_reaches_manager_with_attachment(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        client = mock.Mock()
        client.media_show.return_value = {
            "download_url": "https://download.example/media-only",
            "filename": "evidence.pdf",
        }

        with (
            mock.patch.object(
                interface.ingest,
                "_download_url",
                return_value=str(Path(self.tmp.name) / "media" / "evt-album_evidence.pdf"),
            ),
            mock.patch("diagnostics.store.Diagnostics.get_active_run", return_value=None),
            mock.patch("diagnostics.store.Diagnostics.start_run", side_effect=RuntimeError),
        ):
            processed = interface.process_incoming_event(
                {
                    "type": "m.album",
                    "event_id": "evt-album",
                    "room_id": "room-a",
                    "content": {
                        "items": [
                            {"media_id": "media-only", "filename": "evidence.pdf"},
                        ]
                    },
                },
                client=client,
            )

        self.assertEqual(processed[0], "carbon-a")
        self.assertIn("event_type: m.album", processed[1])
        self.assertIn("downloaded_files:", processed[1])
        self.assertIn("evt-album_evidence.pdf", processed[1])
        self.assertNotIn("message:\n", processed[1])

    def test_ignored_events_update_local_watermark(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        event = {"type": "m.progress", "event_id": "evt-progress", "room_id": "room-a"}

        processed = interface.process_incoming_event(event, client=object())

        self.assertIsNone(processed)
        contact = interface.get_contact("carbon-a")
        self.assertEqual(contact["last_polled_event_id"], "evt-progress")
        self.assertEqual(interface.get_contacts()["last_seen_event_id"], "evt-progress")

    def test_self_sender_handle_updates_watermark_and_drops_echo(self):
        state = interface.get_contacts()
        state["own_ids"] = ["api-dev-test"]
        interface.state._save_state(state)
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
        self.assertEqual(interface.get_contacts()["last_seen_event_id"], "evt-self")

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
        self.assertEqual(interface.get_contacts()["last_seen_event_id"], "evt-new")

    def test_get_unread_events_consumes_and_commits_durable_inbox(self):
        class FakeClient:
            def __init__(self):
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

            def read(self, room_id, event_id):
                self.read_calls.append((room_id, event_id))

        frame = {
            "type": "event",
            "room_id": "room-a",
            "event": {
                "type": "m.text",
                "event_id": "evt-sync",
                "content": {"body": "hello from durable inbox"},
            },
        }
        interface.constants.DEFAULT_INBOX_FILE.write_text(
            json.dumps(frame) + "\n",
            encoding="utf-8",
        )
        interface.inbox._queue_inbox_records(
            interface.inbox._read_new_inbox_records(interface.constants.DEFAULT_INBOX_FILE)
        )
        fake = FakeClient()
        with (
            mock.patch.object(interface.client, "InterfaceClient", return_value=fake),
            mock.patch.object(interface.inbox, "start_listener"),
            mock.patch(
                "interface.work.replay_pending_call_updates",
                return_value=0,
            ) as replay_calls,
        ):
            contexts = interface.get_unread_events()

        replay_calls.assert_called_once_with()
        self.assertIn("carbon-a", contexts)
        self.assertIn("hello from durable inbox", contexts["carbon-a"])
        self.assertEqual(interface.get_contacts()["last_seen_event_id"], "evt-sync")
        consumer = json.loads(
            interface.constants.INBOX_CONSUMER_FILE.read_text(encoding="utf-8")
        )
        self.assertEqual(
            consumer["offset"],
            interface.constants.DEFAULT_INBOX_FILE.stat().st_size,
        )

    def test_uncommitted_inbox_record_replays_after_restart(self):
        frames = [
            {"type": "event", "room_id": "room-a", "event": {"event_id": "evt-1"}},
            {"type": "event", "room_id": "room-a", "event": {"event_id": "evt-2"}},
        ]
        interface.constants.DEFAULT_INBOX_FILE.write_text(
            "".join(json.dumps(frame) + "\n" for frame in frames),
            encoding="utf-8",
        )

        records = interface.inbox._read_new_inbox_records(interface.constants.DEFAULT_INBOX_FILE)
        self.assertEqual([r.frame["event"]["event_id"] for r in records], ["evt-1", "evt-2"])
        interface.inbox._commit_inbox_record(records[0])

        with interface.inbox_file._inbox_scan_lock:
            interface.inbox_file._inbox_scan_state.clear()
        replay = interface.inbox._read_new_inbox_records(interface.constants.DEFAULT_INBOX_FILE)

        self.assertEqual([r.frame["event"]["event_id"] for r in replay], ["evt-2"])

    def test_call_journal_failure_replays_before_inbox_cursor_advances(self):
        interface.upsert_contact(
            "silicon",
            "silicon-b",
            room_id="room-b",
            display_name="Babbage",
        )
        frame = {
            "type": "event",
            "room_id": "room-b",
            "event": {
                "type": "m.text",
                "event_id": "event-b-1",
                "sender_id": "silicon-b",
                "content": {"body": "Please review this."},
            },
        }
        interface.constants.DEFAULT_INBOX_FILE.write_text(
            json.dumps(frame) + "\n",
            encoding="utf-8",
        )
        interface.inbox._queue_inbox_records(
            interface.inbox._read_new_inbox_records(interface.constants.DEFAULT_INBOX_FILE)
        )
        fake = mock.Mock()
        bookkeeping = mock.Mock(
            side_effect=[
                interface.CallBookkeepingError("disk unavailable"),
                None,
            ]
        )
        with (
            mock.patch.object(interface.client, "InterfaceClient", return_value=fake),
            mock.patch.object(interface.inbox, "start_listener"),
            mock.patch.object(
                interface.contacts,
                "discover_rooms",
                return_value=interface.get_contacts(),
            ),
            mock.patch.object(
                interface.ingest,
                "_record_incoming_call_bookkeeping",
                bookkeeping,
            ),
            mock.patch(
                "interface.work.replay_pending_call_updates",
                return_value=0,
            ),
        ):
            self.assertEqual(interface.get_unread_events(), {})
            self.assertFalse(interface.constants.INBOX_CONSUMER_FILE.exists())
            recovered = interface.get_unread_events()

        self.assertIn("Please review this.", recovered["silicon-b"])
        self.assertEqual(bookkeeping.call_count, 2)
        consumer = json.loads(
            interface.constants.INBOX_CONSUMER_FILE.read_text(encoding="utf-8")
        )
        self.assertEqual(
            consumer["offset"],
            interface.constants.DEFAULT_INBOX_FILE.stat().st_size,
        )

    def test_lost_root_acceptance_response_replays_without_duplicate_turn(self):
        from manager.runtime.maintenance import MaintenanceCoordinator

        interface.upsert_contact(
            "carbon",
            "carbon-a",
            room_id="room-a",
            display_name="Carbon A",
        )
        frame = {
            "type": "event",
            "room_id": "room-a",
            "event": {
                "type": "m.text",
                "event_id": "event-a-1",
                "sender_id": "carbon-a",
                "content": {"body": "Run this exactly once."},
            },
        }
        interface.constants.DEFAULT_INBOX_FILE.write_text(
            json.dumps(frame) + "\n",
            encoding="utf-8",
        )
        interface.inbox._queue_inbox_records(
            interface.inbox._read_new_inbox_records(interface.constants.DEFAULT_INBOX_FILE)
        )
        coordinator = MaintenanceCoordinator(
            self.tmp.name,
            state_file=Path(self.tmp.name) / "maintenance.json",
        )
        original_enqueue = coordinator.enqueue_ingress_root

        def commit_then_lose_response(*args, **kwargs):
            original_enqueue(*args, **kwargs)
            raise OSError("response lost")

        with (
            mock.patch.object(interface.client, "InterfaceClient", return_value=mock.Mock()),
            mock.patch.object(interface.inbox, "start_listener"),
            mock.patch.object(
                interface.contacts,
                "discover_rooms",
                return_value=interface.get_contacts(),
            ),
            mock.patch("helpers.process.submit_best_effort", return_value=True),
            mock.patch(
                "interface.work.replay_pending_call_updates",
                return_value=0,
            ),
            mock.patch(
                "diagnostics.store.Diagnostics.get_active_run",
                return_value=None,
            ),
            mock.patch(
                "diagnostics.store.Diagnostics.start_run",
                side_effect=RuntimeError,
            ),
            mock.patch("manager.runtime.maintenance.COORDINATOR", coordinator),
            mock.patch.object(
                coordinator,
                "enqueue_ingress_root",
                side_effect=commit_then_lose_response,
            ),
        ):
            self.assertEqual(interface.get_unread_events_durable(), {})

        self.assertFalse(interface.constants.INBOX_CONSUMER_FILE.exists())
        first_turn = coordinator.claim_pending_roots()
        self.assertEqual(len(first_turn), 1)
        self.assertIn("Run this exactly once.", first_turn[0].context)
        coordinator.complete_roots(first_turn)

        with (
            mock.patch.object(interface.client, "InterfaceClient", return_value=mock.Mock()),
            mock.patch.object(interface.inbox, "start_listener"),
            mock.patch.object(
                interface.contacts,
                "discover_rooms",
                return_value=interface.get_contacts(),
            ),
            mock.patch("helpers.process.submit_best_effort", return_value=True),
            mock.patch(
                "interface.work.replay_pending_call_updates",
                return_value=0,
            ),
            mock.patch(
                "diagnostics.store.Diagnostics.get_active_run",
                return_value=None,
            ),
            mock.patch(
                "diagnostics.store.Diagnostics.start_run",
                side_effect=RuntimeError,
            ),
            mock.patch("manager.runtime.maintenance.COORDINATOR", coordinator),
        ):
            self.assertEqual(interface.get_unread_events_durable(), {})

        consumer = json.loads(
            interface.constants.INBOX_CONSUMER_FILE.read_text(encoding="utf-8")
        )
        self.assertEqual(
            consumer["offset"],
            interface.constants.DEFAULT_INBOX_FILE.stat().st_size,
        )
        self.assertEqual(coordinator.claim_pending_roots(), [])

    def test_initial_snapshot_timeline_is_routed_through_same_dedupe_path(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        snapshot = {
            "type": "initial.snapshot",
            "rooms": [
                {
                    "room_id": "room-a",
                    "timeline": {
                        "events": [
                            {
                                "type": "m.text",
                                "event_id": "evt-snapshot",
                                "content": {"body": "recovered at the barrier"},
                            }
                        ]
                    },
                }
            ],
        }
        fake = mock.Mock()
        fake.whoami.return_value = {"silicon_id": "self-si"}
        fake.rooms_list.return_value = {"rooms": []}
        interface.inbox._event_queue.put(interface.InboxRecord(snapshot))

        with (
            mock.patch.object(interface.client, "InterfaceClient", return_value=fake),
            mock.patch.object(interface.inbox, "start_listener"),
            mock.patch.object(interface.contacts, "discover_rooms", return_value=interface.get_contacts()),
        ):
            contexts = interface.get_unread_events()

        self.assertIn("recovered at the barrier", contexts["carbon-a"])

    def test_daemon_status_rejects_pre_v2_contract(self):
        client = interface.InterfaceClient.__new__(interface.InterfaceClient)
        with mock.patch.object(client, "run", return_value={"text": "unknown command"}):
            with self.assertRaisesRegex(interface.InterfaceError, "CLI v2"):
                client.daemon_status()

    def test_runtime_wakeup_is_not_lost_before_wait_begins(self):
        interface.notify_runtime_activity()

        self.assertTrue(interface.wait_for_runtime_activity(0))
        self.assertFalse(interface.wait_for_runtime_activity(0))

    def test_runtime_file_change_wakes_main_condition(self):
        state_file = Path(self.tmp.name) / "maintenance.json"
        state_file.write_text("{}", encoding="utf-8")
        interface.start_runtime_file_watch(state_file)
        deadline = time.monotonic() + 1
        while (
            not interface.runtime_file_notifications_active()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        replacement = state_file.with_suffix(".tmp")
        replacement.write_text('{"phase":"draining"}', encoding="utf-8")
        replacement.replace(state_file)

        self.assertTrue(interface.wait_for_runtime_activity(1.5))

    def test_listener_queues_appended_inbox(self):
        interface.constants.DEFAULT_INBOX_FILE.write_text("", encoding="utf-8")
        ready = threading.Event()
        stop = threading.Event()

        class FakeClient:
            def daemon_local_status(self):
                ready.set()
                return {
                    "running": True,
                    "inbox": str(interface.constants.DEFAULT_INBOX_FILE),
                }

            def daemon_status(self):
                return {
                    "running": True,
                    "inbox": str(interface.constants.DEFAULT_INBOX_FILE),
                    "cursors": {},
                }

        with mock.patch.object(interface.client, "InterfaceClient", FakeClient):
            thread = threading.Thread(
                target=interface.inbox._listener_loop,
                args=(stop,),
                daemon=True,
            )
            thread.start()
            try:
                self.assertTrue(ready.wait(1))
                with interface.constants.DEFAULT_INBOX_FILE.open(
                    "a",
                    encoding="utf-8",
                ) as inbox:
                    inbox.write('{"type":"test-notification"}\n')
                    inbox.flush()
                record = interface.inbox._event_queue.get(timeout=1)
            finally:
                stop.set()
                # Join authoritatively. A listener that outlives this test goes
                # on to hit the real Interface CLI once the client patch lifts,
                # and prints into whichever test is running then.
                thread.join(10)
                self.assertFalse(thread.is_alive(), "listener thread leaked")

        self.assertFalse(thread.is_alive())
        self.assertEqual(record.frame["type"], "test-notification")

    def test_listener_uses_local_health_between_deep_contract_probes(self):
        stop = threading.Event()
        calls = {"local": 0, "deep": 0, "wait": 0}

        class FakeClient:
            def daemon_local_status(self):
                calls["local"] += 1
                return {
                    "running": True,
                    "inbox": str(interface.constants.DEFAULT_INBOX_FILE),
                }

            def daemon_status(self):
                calls["deep"] += 1
                return {
                    "running": True,
                    "inbox": str(interface.constants.DEFAULT_INBOX_FILE),
                    "cursors": {},
                }

        class FakeWaiter:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def wait(self, _timeout, stop_event):
                calls["wait"] += 1
                time.sleep(0.015)
                if calls["wait"] >= 3:
                    stop_event.set()

        with (
            mock.patch.object(interface.client, "InterfaceClient", FakeClient),
            mock.patch.object(interface.inbox, "PathChangeWaiter", FakeWaiter),
            mock.patch.object(
                interface.inbox,
                "_read_new_inbox_records",
                return_value=[],
            ),
            mock.patch.object(interface.inbox, "DAEMON_HEALTH_SECONDS", 0.01),
            mock.patch.object(interface.inbox, "DAEMON_DEEP_HEALTH_SECONDS", 60),
            mock.patch.object(
                interface.inbox,
                "DAEMON_DEEP_HEALTH_JITTER_SECONDS",
                0,
            ),
        ):
            interface.inbox._listener_loop(stop)

        self.assertGreaterEqual(calls["local"], 3)
        self.assertEqual(calls["deep"], 1)

    def test_reply_segments_keep_order_and_report_missing_files(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        existing = Path(self.tmp.name) / "file.txt"
        existing.write_text("ok", encoding="utf-8")

        calls = []

        class FakeClient:
            def send(self, room_id, message, **_kwargs):
                calls.append(("send", room_id, message))

            def send_file(self, room_id, path):
                calls.append(("send_file", room_id, path))

            def tts(self, room_id, text):
                calls.append(("tts", room_id, text))

        with mock.patch.object(interface.client, "InterfaceClient", FakeClient):
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

    def test_reply_uses_deterministic_client_id_per_parsed_segment(self):
        interface.upsert_contact("carbon", "carbon-a", room_id="room-a")
        sends = []

        class FakeClient:
            def send(self, room_id, message, **kwargs):
                sends.append((room_id, message, kwargs))
                return {"event_id": f"event-{len(sends)}"}

            def tts(self, room_id, text):
                return {"event_id": "voice-1"}

        with mock.patch.object(interface.client, "InterfaceClient", FakeClient):
            self.assertEqual(
                interface.reply_contact(
                    "first [voice=pause] second",
                    "carbon-a",
                    client_id="final-reply-1",
                ),
                "Message sent",
            )

        self.assertEqual(
            [value[2]["client_id"] for value in sends],
            [
                "final-reply-1:segment:1:text",
                "final-reply-1:segment:3:text",
            ],
        )

    def test_multi_text_silicon_reply_only_terminalizes_final_segment(self):
        interface.upsert_contact(
            "silicon",
            "silicon-b",
            room_id="room-b",
            display_name="Babbage",
        )
        text_events = iter(("event-text-1", "event-text-2"))

        class FakeClient:
            def send(self, _room_id, _message, **_kwargs):
                return {"event_id": next(text_events)}

            def tts(self, _room_id, _text):
                return {"event_id": "event-voice-1"}

        with (
            mock.patch.object(interface.client, "InterfaceClient", FakeClient),
            mock.patch.object(
                interface.outbound,
                "_record_sent_call_message",
            ) as record,
        ):
            self.assertEqual(
                interface.reply_contact(
                    "First part. [voice=pause] Final part.",
                    "silicon-b",
                ),
                "Message sent",
            )

        self.assertEqual(
            record.call_args_list,
            [
                mock.call(
                    "silicon-b",
                    "First part.",
                    "event-text-1",
                    terminal=False,
                ),
                mock.call(
                    "silicon-b",
                    "Final part.",
                    "event-text-2",
                    terminal=True,
                ),
            ],
        )

    def test_accepted_silicon_text_creates_card_when_no_call_exists(self):
        reference = {
            "owner_contact_id": "silicon-b",
            "call_id": "call-a",
            "work_event_id": "event-a",
            "target_kind": "silicon",
            "target_id": "silicon-b",
        }
        with (
            mock.patch(
                "interface.work.record_contact_call_message",
                return_value=False,
            ),
            mock.patch(
                "interface.work.prepare_outbound_call",
                return_value=reference,
            ) as prepare,
            mock.patch(
                "interface.work.enqueue_outbound_call",
                return_value=True,
            ) as enqueue,
            mock.patch.object(
                interface.outbound,
                "get_contact",
                return_value={
                    "contact_type": "silicon",
                    "silicon_id": "silicon-b",
                    "display_name": "Babbage",
                },
            ),
            mock.patch.object(
                interface.outbound,
                "get_own_profile",
                return_value={
                    "silicon_id": "silicon-a",
                    "name": "Ada",
                },
            ),
        ):
            interface.outbound._record_sent_call_message(
                "silicon-b",
                "Hello Babbage.",
                "event-sent-1",
            )

        prepare.assert_called_once()
        enqueue.assert_called_once_with(
            reference,
            target_name="Babbage",
            message="Hello Babbage.",
            idempotency_key="outgoing-call:silicon-b:event-sent-1",
        )

    def test_accepted_silicon_text_appends_without_creating_second_card(self):
        with (
            mock.patch(
                "interface.work.record_contact_call_message",
                return_value=True,
            ) as append,
            mock.patch(
                "interface.work.prepare_outbound_call",
            ) as prepare,
            mock.patch.object(
                interface.outbound,
                "get_own_profile",
                return_value={
                    "silicon_id": "silicon-a",
                    "name": "Ada",
                },
            ),
        ):
            interface.outbound._record_sent_call_message(
                "silicon-b",
                "Existing call answer.",
                "event-sent-2",
            )

        self.assertEqual(
            append.call_args.kwargs["idempotency_key"],
            "outgoing-call:silicon-b:event-sent-2",
        )
        prepare.assert_not_called()

    def test_outgoing_silicon_bookkeeping_recovers_from_durable_self_event(self):
        interface.upsert_contact(
            "silicon",
            "silicon-b",
            room_id="room-b",
            display_name="Babbage",
        )
        state = interface.get_contacts()
        state["own_ids"] = ["silicon-self"]
        interface.state._save_state(state)

        class FakeClient:
            def send(self, _room_id, _message, **_kwargs):
                return {"event_id": "event-outgoing-1"}

        bookkeeping = mock.Mock(
            side_effect=[
                interface.CallBookkeepingError("disk unavailable"),
                None,
            ]
        )
        with (
            mock.patch.object(interface.client, "InterfaceClient", FakeClient),
            mock.patch.object(
                interface.outbound,
                "_record_sent_call_message",
                bookkeeping,
            ),
        ):
            self.assertEqual(
                interface.reply_contact("Hello.", "silicon-b"),
                "Message sent",
            )
            self.assertIsNone(
                interface.process_incoming_event(
                    {
                        "type": "m.text",
                        "event_id": "event-outgoing-1",
                        "room_id": "room-b",
                        "sender_id": "silicon-self",
                        "content": {"body": "Hello."},
                    },
                    client=FakeClient(),
                )
            )

        self.assertEqual(bookkeeping.call_count, 2)
        self.assertEqual(
            bookkeeping.call_args_list[1].args,
            ("silicon-b", "Hello.", "event-outgoing-1"),
        )


class InterfaceClientTest(unittest.TestCase):
    @staticmethod
    def _serve_rpc_once(root: Path, responder):
        state = root / ".silicon-interface"
        state.mkdir(parents=True, exist_ok=True)
        socket_path = state / "daemon.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)

        def run():
            connection, _ = server.accept()
            try:
                request = b""
                while b"\n" not in request:
                    chunk = connection.recv(64 * 1024)
                    if not chunk:
                        break
                    request += chunk
                response = responder(json.loads(request.split(b"\n", 1)[0]))
                if response is not None:
                    connection.sendall(
                        json.dumps(response, separators=(",", ":")).encode("utf-8")
                        + b"\n"
                    )
            finally:
                connection.close()
                server.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_rpc_is_preferred_over_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def respond(request):
                return {
                    "version": 1,
                    "id": request["id"],
                    "ok": True,
                    "result": {"silicon_id": "silicon-one"},
                }

            thread = self._serve_rpc_once(root, respond)
            with mock.patch("subprocess.run") as run:
                payload = interface.InterfaceClient(cwd=root).whoami()
            thread.join(1)

            self.assertEqual(payload, {"silicon_id": "silicon-one"})
            run.assert_not_called()

    def test_rpc_does_not_retry_ambiguous_mutation_through_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            thread = self._serve_rpc_once(root, lambda _request: None)
            with mock.patch("subprocess.run") as run:
                with self.assertRaisesRegex(
                    interface.InterfaceError,
                    "closed without a response",
                ):
                    interface.InterfaceClient(cwd=root).send("room-1", "hello")
            thread.join(1)
            run.assert_not_called()

    def test_rpc_preserves_structured_api_error_for_work_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def respond(request):
                return {
                    "version": 1,
                    "id": request["id"],
                    "ok": False,
                    "error": {
                        "code": "API_ERROR",
                        "status": 409,
                        "message": "revision conflict",
                        "body": {
                            "code": "revision_conflict",
                            "current": {"revision": 7},
                        },
                    },
                }

            thread = self._serve_rpc_once(root, respond)
            client = interface.InterfaceClient(cwd=root)
            with self.assertRaises(interface.WorkCallMutationError) as raised:
                client.work_call_patch("task-1", "call-1", {"revision": 6})
            thread.join(1)

            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(raised.exception.code, "revision_conflict")
            self.assertEqual(raised.exception.current_revision, 7)

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
                client.progress(
                    "room-1",
                    "manager",
                    "thinking",
                    "running",
                    "frame-1",
                )

            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn([str(exe), "--json", "me"], commands)
            self.assertIn([str(exe), "--json", "rooms", "show", "room-1", "--limit", "0"], commands)
            self.assertIn([str(exe), "--json", "rooms", "direct", "carbon", "saket"], commands)
            self.assertIn(
                [
                    str(exe),
                    "--json",
                    "progress",
                    "room-1",
                    "thinking",
                    "--group",
                    "manager",
                    "--note",
                    "running",
                    "--frame",
                    "frame-1",
                ],
                commands,
            )

    def test_json_parser_uses_last_json_line(self):
        self.assertEqual(interface.rpc._parse_json_output("noise\n{\"ok\": true}\n"), {"ok": True})

    def test_remote_browser_url_parser(self):
        self.assertEqual(
            interface.parse_remote_browser_url("Share URL: https://remote.example/session/abc\nexpires soon"),
            "https://remote.example/session/abc",
        )



class GlassProfileSyncTest(InterfaceStateTest):
    def test_profile_central_carbon_does_not_bypass_trust_policy_projection(self):
        interface.upsert_contact("carbon", "lord-1", room_id="room-lord")
        interface.upsert_contact("carbon", "alice", room_id="room-alice")

        interface.contacts._sync_profile_from_glass(
            {"silicon_id": "self", "central_carbon": {"carbon_id": "alice", "username": "alice", "name": "Alice"}}
        )

        self.assertFalse(interface.get_contact("alice")["is_central_carbon"])
        self.assertEqual(interface.get_contact("alice")["trust_level"], "very_low")
        self.assertFalse(interface.get_contact("lord-1")["is_central_carbon"])
        self.assertEqual(interface.get_contact("lord-1")["trust_level"], "very_low")
        self.assertEqual(
            interface.get_own_profile()["central_carbon"]["carbon_id"],
            "alice",
        )

    def test_absent_central_carbon_key_keeps_trust_projection_unchanged(self):
        interface.upsert_contact("carbon", "alice", room_id="room-a")
        interface.contacts._sync_profile_from_glass({"silicon_id": "self", "name": "Ada Silicon"})
        self.assertFalse(interface.get_contact("alice")["is_central_carbon"])
        self.assertEqual(interface.get_contact("alice")["trust_level"], "very_low")

    def test_profile_caches_description_for_prompts(self):
        interface.contacts._sync_profile_from_glass(
            {
                "silicon_id": "self",
                "name": "Ada Silicon",
                "tagline": "designs systems",
                "description": "Handles inbound sales emails.",
                "architecture_node_id": "SALES",
                "job_description": "Qualify inbound leads and own sales follow-up.",
                "advertising_memory_path": "prompts/advertising/self.md",
                "owner_team_slug": "revenue",
                "central_carbon": None,
            }
        )
        profile = interface.get_own_profile()
        self.assertEqual(profile["silicon_id"], "self")
        self.assertEqual(profile["description"], "Handles inbound sales emails.")
        self.assertEqual(profile["architecture_node_id"], "SALES")
        self.assertEqual(
            profile["job_description"],
            "Qualify inbound leads and own sales follow-up.",
        )
        self.assertEqual(
            profile["advertising_memory_path"],
            "prompts/advertising/self.md",
        )
        self.assertEqual(profile["owner_team_slug"], "revenue")
        self.assertEqual(profile["team"], "revenue")
        self.assertEqual(profile["central_carbon"], None)

    def test_partial_profile_payload_preserves_last_known_role_metadata(self):
        interface.contacts._sync_profile_from_glass(
            {
                "silicon_id": "self",
                "name": "Ada Silicon",
                "description": "Handles inbound sales emails.",
                "job_description": "Qualify inbound leads.",
                "owner_team_slug": "revenue",
                "advertising_memory_path": "prompts/advertising/self.md",
                "central_carbon": {
                    "carbon_id": "alice",
                    "name": "Alice",
                },
            }
        )

        interface.contacts._sync_profile_from_glass(
            {
                "silicon_id": "self",
                "tagline": "available",
            }
        )

        profile = interface.get_own_profile()
        self.assertEqual(profile["description"], "Handles inbound sales emails.")
        self.assertEqual(profile["job_description"], "Qualify inbound leads.")
        self.assertEqual(profile["owner_team_slug"], "revenue")
        self.assertEqual(
            profile["advertising_memory_path"],
            "prompts/advertising/self.md",
        )
        self.assertEqual(profile["central_carbon"]["carbon_id"], "alice")
        self.assertEqual(profile["tagline"], "available")

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
        # The profile is cached, but only the trust-policy endpoint can grant
        # central/ultimate trust.
        self.assertFalse(state["contacts"]["carbon-a"]["is_central_carbon"])
        self.assertFalse(state["contacts"]["carbon-b"]["is_central_carbon"])
        self.assertEqual(state["contacts"]["carbon-b"]["trust_level"], "very_low")
        self.assertEqual(state["profile"]["description"], "Sales silicon.")


if __name__ == "__main__":
    unittest.main()
