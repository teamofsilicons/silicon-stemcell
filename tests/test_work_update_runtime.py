import json
import math
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import interface
import interface.client
import interface.contacts
import interface.events
import interface.inbox
import interface.ingest
import interface.outbound
import interface.remote_browser
import interface.state
import interface.work_updates as work_updates
from helpers.process import flush_best_effort


NOW = "2026-07-23T06:00:00Z"


class FakeWorkClient:
    """Small Glass-shaped fake for the manager's durable-update adapter."""

    def __init__(self):
        self.tasks = {}
        self.events = {}
        self.requests = []
        self.reads = []

    def _record(self, name, *ids, payload=None):
        self.requests.append((name, ids, deepcopy(payload or {})))

    def payloads(self, name):
        return [
            payload
            for request_name, _ids, payload in self.requests
            if request_name == name
        ]

    def _task(self, task_id):
        return self.tasks[task_id]

    def _bump_task(self, task_id):
        task = self._task(task_id)
        task["revision"] += 1
        task["updated_at"] = NOW
        return task

    def _event(self, task_id, payload):
        task = self._task(task_id)
        event = {
            "schema_version": 1,
            "task_id": task_id,
            "room_id": task["room_id"],
            "task_title": task["title"],
            "body": "",
            "blocks": [],
            "revision": 0,
            "history": [],
            "timing": {
                "estimate_seconds": task["estimate_seconds"],
                "active_elapsed_seconds": task["active_elapsed_seconds"],
                "timer_state": task["timer_state"],
            },
            "created_at": NOW,
            "updated_at": NOW,
        }
        event.update(
            {
                key: deepcopy(value)
                for key, value in payload.items()
                if key not in {"client_id", "revision"}
            }
        )
        self.events[(task_id, event["work_event_id"])] = event
        return event

    def _event_by(self, task_id, key, value):
        for (candidate_task, _event_id), event in self.events.items():
            if candidate_task == task_id and event.get(key) == value:
                return event
        raise KeyError((task_id, key, value))

    def work_task_create(self, payload):
        self._record("work_task_create", payload=payload)
        todos = []
        for source in payload.get("todos") or []:
            todo = deepcopy(source)
            todo.setdefault("state", "yet_to_start")
            todo.setdefault("description", "")
            todo.setdefault("revision", 0)
            todo.setdefault("history", [])
            todos.append(todo)
        realistic = payload.get("realistic_estimate_seconds")
        estimate = payload.get("estimate_seconds")
        if estimate is None and realistic is not None:
            estimate = math.ceil(realistic * 1.05)
        task = {
            "schema_version": payload.get("schema_version", 1),
            "room_id": payload["room_id"],
            "task_id": payload["task_id"],
            "title": payload["title"],
            "description": payload.get("description", ""),
            "state": payload.get("state", "running"),
            "estimate_seconds": estimate or 0,
            "active_elapsed_seconds": 0,
            "timer_state": "running",
            "revision": 0,
            "todos": todos,
            "history": [],
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.tasks[task["task_id"]] = task
        return deepcopy(task)

    def work_task_show(self, task_id):
        self._record("work_task_show", task_id)
        return deepcopy(self._task(task_id))

    def work_task_patch(self, task_id, payload):
        self._record("work_task_patch", task_id, payload=payload)
        task = self._task(task_id)
        if "revision" in payload:
            assert payload["revision"] == task["revision"]
        for key, value in payload.items():
            if key not in {"revision", "client_id"}:
                if value is None and key == "timer_pause_reason":
                    task.pop(key, None)
                else:
                    task[key] = deepcopy(value)
        return deepcopy(self._bump_task(task_id))

    def work_todo_add(self, task_id, payload):
        self._record("work_todo_add", task_id, payload=payload)
        todo = {
            **{
                key: deepcopy(value)
                for key, value in payload.items()
                if key != "client_id"
            },
            "revision": 0,
            "history": [],
        }
        todo.setdefault("state", "yet_to_start")
        todo.setdefault("description", "")
        self._task(task_id)["todos"].append(todo)
        return deepcopy(self._bump_task(task_id))

    def work_todo_patch(self, task_id, todo_id, payload):
        self._record("work_todo_patch", task_id, todo_id, payload=payload)
        todo = next(
            row for row in self._task(task_id)["todos"] if row["todo_id"] == todo_id
        )
        if "revision" in payload:
            assert payload["revision"] == todo["revision"]
        for key, value in payload.items():
            if key not in {"revision", "client_id"}:
                todo[key] = deepcopy(value)
        todo["revision"] += 1
        return deepcopy(self._bump_task(task_id))

    def work_milestone_create(self, task_id, payload):
        self._record("work_milestone_create", task_id, payload=payload)
        return deepcopy(self._event(task_id, payload))

    def work_blocker_create(self, task_id, payload):
        self._record("work_blocker_create", task_id, payload=payload)
        task = self._task(task_id)
        task.update(
            {
                "state": "blocked",
                "timer_state": "paused",
                "timer_pause_reason": "blocker",
            }
        )
        self._bump_task(task_id)
        event = self._event(task_id, payload)
        event["timing"]["timer_state"] = "paused"
        event["timing"]["timer_pause_reason"] = "blocker"
        return deepcopy(event)

    def work_blocker_resolve(self, task_id, blocker_id, payload):
        self._record("work_blocker_resolve", task_id, blocker_id, payload=payload)
        event = self._event_by(task_id, "blocker_id", blocker_id)
        if "revision" in payload:
            assert payload["revision"] == event["revision"]
        event.update(
            {
                key: deepcopy(value)
                for key, value in payload.items()
                if key not in {"revision", "client_id"}
            }
        )
        event["revision"] += 1
        event["resolved_at"] = NOW
        event["updated_at"] = NOW
        if not any(
            candidate.get("kind") == "blocker"
            and candidate.get("state") == "open"
            for (candidate_task, _), candidate in self.events.items()
            if candidate_task == task_id
        ):
            task = self._task(task_id)
            task["state"] = "running"
            task["timer_state"] = "running"
            task.pop("timer_pause_reason", None)
            self._bump_task(task_id)
        return deepcopy(event)

    def work_worker_group_create(self, task_id, payload):
        self._record("work_worker_group_create", task_id, payload=payload)
        return deepcopy(self._event(task_id, payload))

    def work_worker_group_patch(self, task_id, group_id, payload):
        self._record("work_worker_group_patch", task_id, group_id, payload=payload)
        event = self._event_by(task_id, "group_id", group_id)
        if "revision" in payload:
            assert payload["revision"] == event["revision"]
        event.update(
            {
                key: deepcopy(value)
                for key, value in payload.items()
                if key not in {"revision", "client_id"}
            }
        )
        event["revision"] += 1
        return deepcopy(event)

    def work_worker_create(self, task_id, group_id, payload):
        self._record("work_worker_create", task_id, group_id, payload=payload)
        event = self._event_by(task_id, "group_id", group_id)
        worker = {
            **{
                key: deepcopy(value)
                for key, value in payload.items()
                if key not in {"client_id", "revision"}
            },
            "revision": 0,
        }
        event.setdefault("workers", []).append(worker)
        event["revision"] += 1
        return deepcopy(event)

    def work_worker_patch(self, task_id, group_id, invocation_id, payload):
        self._record(
            "work_worker_patch",
            task_id,
            group_id,
            invocation_id,
            payload=payload,
        )
        event = self._event_by(task_id, "group_id", group_id)
        worker = next(
            row
            for row in event["workers"]
            if row["invocation_id"] == invocation_id
        )
        if "revision" in payload:
            assert payload["revision"] == worker["revision"]
        for key, value in payload.items():
            if key not in {"revision", "client_id"}:
                worker[key] = deepcopy(value)
        worker["revision"] += 1
        event["revision"] += 1
        return deepcopy(event)

    def work_call_create(self, task_id, payload):
        self._record("work_call_create", task_id, payload=payload)
        return deepcopy(self._event(task_id, payload))

    def work_standalone_call_create(self, payload):
        self._record("work_standalone_call_create", payload=payload)
        event = {
            "schema_version": 1,
            "task_id": None,
            "room_id": payload["room_id"],
            "task_title": None,
            "body": "",
            "blocks": [],
            "revision": 0,
            "history": [],
            "timing": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
        event.update(
            {
                key: deepcopy(value)
                for key, value in payload.items()
                if key not in {"client_id", "revision"}
            }
        )
        self.events[("", event["work_event_id"])] = event
        return deepcopy(event)

    def work_call_patch(self, task_id, call_id, payload):
        self._record("work_call_patch", task_id, call_id, payload=payload)
        event = self._event_by(task_id, "call_id", call_id)
        if "revision" in payload:
            assert payload["revision"] == event["revision"]
        for key, value in payload.items():
            if key in {"revision", "client_id", "transcript"}:
                continue
            event[key] = deepcopy(value)
        existing = {
            row["transcript_id"]: row for row in event.setdefault("transcript", [])
        }
        for row in payload.get("transcript") or []:
            existing[row["transcript_id"]] = deepcopy(row)
        event["transcript"] = list(existing.values())
        event["revision"] += 1
        return deepcopy(event)

    def work_standalone_call_patch(self, call_id, payload):
        self._record("work_standalone_call_patch", call_id, payload=payload)
        event = self._event_by("", "call_id", call_id)
        if "revision" in payload:
            assert payload["revision"] == event["revision"]
        for key, value in payload.items():
            if key in {"revision", "client_id", "transcript"}:
                continue
            event[key] = deepcopy(value)
        existing = {
            row["transcript_id"]: row for row in event.setdefault("transcript", [])
        }
        for row in payload.get("transcript") or []:
            existing[row["transcript_id"]] = deepcopy(row)
        event["transcript"] = list(existing.values())
        event["revision"] += 1
        return deepcopy(event)

    def work_task_transition(self, task_id, transition, payload):
        self._record(
            "work_task_transition",
            task_id,
            transition,
            payload=payload,
        )
        final_state = {
            "complete": "completed",
            "fail": "failed",
            "cancel": "cancelled",
        }[transition]
        task = self._task(task_id)
        task["state"] = final_state
        task["timer_state"] = "stopped"
        task.pop("timer_pause_reason", None)
        self._bump_task(task_id)
        event = self._event(task_id, payload)
        event["timing"]["timer_state"] = "stopped"
        return deepcopy(event)

    def read(self, room_id, event_id):
        self.reads.append((room_id, event_id))
        return {}


class ExplodingClient:
    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise OSError("Interface is offline")

        return fail


class WorkUpdateRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_work_state = work_updates.WORK_UPDATES_FILE
        self.old_contacts = interface.constants.CONTACTS_FILE
        self.old_backup = interface.constants.CONTACTS_BACKUP_FILE
        self.old_legacy = interface.constants.LEGACY_TELEGRAM_CONTACTS_FILE
        work_updates.WORK_UPDATES_FILE = root / "work_updates.json"
        work_updates._CALL_RETRY_INFLIGHT.clear()
        interface.constants.CONTACTS_FILE = root / "contacts.json"
        interface.constants.CONTACTS_BACKUP_FILE = root / "contacts-backup.json"
        interface.constants.LEGACY_TELEGRAM_CONTACTS_FILE = root / "legacy-contacts.json"
        self.contacts = {
            "carbon-a": {
                "contact_type": "carbon",
                "carbon_id": "carbon-a",
                "room_id": "room-a",
                "display_name": "Ada",
            },
            "carbon-b": {
                "contact_type": "carbon",
                "carbon_id": "carbon-b",
                "room_id": "room-b",
                "display_name": "Babbage",
            },
        }
        self.contact_patch = mock.patch.object(
            work_updates,
            "get_contact",
            side_effect=lambda contact_id: deepcopy(self.contacts.get(contact_id)),
        )
        self.profile_patch = mock.patch.object(
            work_updates,
            "get_own_profile",
            return_value={"name": "Silicon"},
        )
        self.contact_patch.start()
        self.profile_patch.start()

    def tearDown(self):
        self.profile_patch.stop()
        self.contact_patch.stop()
        work_updates.WORK_UPDATES_FILE = self.old_work_state
        work_updates._CALL_RETRY_INFLIGHT.clear()
        interface.constants.CONTACTS_FILE = self.old_contacts
        interface.constants.CONTACTS_BACKUP_FILE = self.old_backup
        interface.constants.LEGACY_TELEGRAM_CONTACTS_FILE = self.old_legacy
        self.tmp.cleanup()

    def _create_task(self, client, contact_id="carbon-a", task_id="task-fitness"):
        return work_updates.WorkUpdates(contact_id, client=client).execute(
            {
                "action": "task/create",
                "data": {
                    "task_id": task_id,
                    "title": "Build a Fitness App",
                    "description": "Build and verify the release.",
                    "realistic_estimate_seconds": 100,
                },
            }
        )

    def test_retry_replay_does_not_rewrite_unchanged_journal(self):
        work_updates._write_state(work_updates._default_state())

        with mock.patch.object(work_updates, "_write_state") as write:
            scheduled = work_updates.replay_pending_call_updates(now=1_000.0)

        self.assertEqual(scheduled, 0)
        write.assert_not_called()

    def test_manager_activity_ids_revisions_and_settlement(self):
        group = work_updates.begin_manager_activity("carbon-a", "run/one")
        self.assertEqual(group, work_updates.begin_manager_activity("carbon-a", "run/one"))
        frame_id, revision, duplicate = work_updates.activity_frame_identity(
            "carbon-a",
            group,
            frame_key="provider-item-1",
            fingerprint="reading:v1",
        )
        same_id, same_revision, duplicate_retry = (
            work_updates.activity_frame_identity(
                "carbon-a",
                group,
                frame_key="provider-item-1",
                fingerprint="reading:v1",
            )
        )
        revised_id, revised_revision, revised_duplicate = (
            work_updates.activity_frame_identity(
                "carbon-a",
                group,
                frame_key="provider-item-1",
                fingerprint="reading:v2",
            )
        )

        self.assertEqual((revision, duplicate), (0, False))
        self.assertEqual((same_id, same_revision, duplicate_retry), (frame_id, 0, True))
        self.assertEqual((revised_id, revised_revision, revised_duplicate), (frame_id, 1, False))
        self.assertEqual(work_updates.current_manager_activity_group("carbon-a"), group)

        work_updates.settle_manager_activity("carbon-a", group)
        self.assertEqual(work_updates.current_manager_activity_group("carbon-a"), "")
        self.assertNotEqual(
            work_updates.begin_manager_activity("carbon-a", "run/two"),
            group,
        )

    def test_progress_transport_rejection_is_diagnostic_and_best_effort(self):
        rejected_client = mock.Mock()
        rejected_client.progress.side_effect = interface.InterfaceError(
            "Glass rejected progress"
        )
        trace = mock.Mock()
        with (
            mock.patch.object(
                interface.outbound,
                "get_contact",
                return_value=self.contacts["carbon-a"],
            ),
            mock.patch.object(
                interface.client,
                "InterfaceClient",
                return_value=rejected_client,
            ),
            mock.patch(
                "diagnostics.store.Diagnostics.get_active_run",
                return_value=trace,
            ),
        ):
            interface.send_progress(
                "carbon-a",
                "manager-run:one",
                "thinking",
                "Reading requirements",
                frame_id="frame-one",
                revision=0,
            )
            self.assertTrue(flush_best_effort())

        trace.event.assert_called_once_with(
            "interface.progress_failed",
            group_id="manager-run:one",
            state="thinking",
            error="Glass rejected progress",
        )

    def test_glass_shaped_task_todo_blocker_worker_call_and_terminal_lifecycle(self):
        client = FakeWorkClient()
        runtime = work_updates.WorkUpdates("carbon-a", client=client)
        created = runtime.execute(
            {
                "action": "task/create",
                "data": {
                    "task_id": "task-fitness",
                    "title": "Build a Fitness App",
                    "description": "Build and verify the release.",
                    "realistic_estimate_seconds": 100,
                    "todos": [
                        {
                            "todo_id": "todo-plan",
                            "title": "Plan the experience",
                            "state": "in_progress",
                        }
                    ],
                },
            }
        )
        self.assertEqual(created["room_id"], "room-a")
        self.assertEqual(created["estimate_seconds"], 105)
        self.assertEqual(work_updates.active_task_id("carbon-a"), "task-fitness")

        runtime.execute(
            {
                "action": "task/update",
                "task_id": "task-fitness",
                "data": {"description": "Planning is complete."},
            }
        )
        runtime.execute(
            {
                "action": "todo/add",
                "task_id": "task-fitness",
                "data": {
                    "todo_id": "todo-build",
                    "title": "Build the application",
                },
            }
        )
        runtime.execute(
            {
                "action": "todo/update",
                "task_id": "task-fitness",
                "todo_id": "todo-build",
                "data": {"state": "completed", "description": "Built."},
            }
        )
        runtime.execute(
            {
                "action": "milestone",
                "task_id": "task-fitness",
                "data": {
                    "work_event_id": "event-ui",
                    "body": "UI/UX is complete.",
                },
            }
        )
        blocker = runtime.execute(
            {
                "action": "blocker/create",
                "task_id": "task-fitness",
                "data": {
                    "work_event_id": "event-colour",
                    "blocker_id": "blocker-colour",
                    "body": "Should the primary colour be red or blue?",
                },
            }
        )
        self.assertEqual(blocker["kind"], "blocker")
        self.assertEqual(client.tasks["task-fitness"]["timer_state"], "paused")
        runtime.execute(
            {
                "action": "blocker/resolve",
                "task_id": "task-fitness",
                "blocker_id": "blocker-colour",
                "data": {"body": "Use blue."},
            }
        )
        self.assertEqual(client.tasks["task-fitness"]["state"], "running")

        runtime.execute(
            {
                "action": "worker-group/create",
                "task_id": "task-fitness",
                "data": {
                    "group_id": "group-build",
                    "work_event_id": "event-workers",
                    "body": "Started workers",
                },
            }
        )
        runtime.execute(
            {
                "action": "worker-group/update",
                "task_id": "task-fitness",
                "group_id": "group-build",
                "data": {"body": "Building in parallel."},
            }
        )
        runtime.execute(
            {
                "action": "worker/create",
                "task_id": "task-fitness",
                "group_id": "group-build",
                "data": {
                    "worker_id": "worker-ui",
                    "invocation_id": "invoke-ui",
                    "name": "UI worker",
                    "state": "in_progress",
                },
            }
        )
        worker_group = runtime.execute(
            {
                "action": "worker/update",
                "task_id": "task-fitness",
                "group_id": "group-build",
                "invocation_id": "invoke-ui",
                "data": {"state": "completed", "description": "UI delivered."},
            }
        )
        self.assertEqual(worker_group["workers"][0]["state"], "completed")

        runtime.execute(
            {
                "action": "call/create",
                "task_id": "task-fitness",
                "data": {
                    "call_id": "call-saket",
                    "work_event_id": "event-call",
                    "direction": "outbound",
                    "target_kind": "manager",
                    "target_id": "saket",
                    "target_name": "Saket's manager",
                    "state": "connecting",
                    "body": "Calling Saket's manager",
                },
            }
        )
        call = runtime.execute(
            {
                "action": "call/update",
                "task_id": "task-fitness",
                "call_id": "call-saket",
                "data": {
                    "state": "completed",
                    "transcript": [
                        {
                            "transcript_id": "transcript-approved",
                            "speaker_kind": "manager",
                            "speaker_id": "manager:saket",
                            "speaker_name": "Saket's manager",
                            "body": "Approved.",
                            "blocks": [],
                            "revision": 0,
                        }
                    ],
                },
            }
        )
        self.assertEqual(call["transcript"][0]["body"], "Approved.")

        terminal = runtime.execute(
            {
                "action": "task/complete",
                "task_id": "task-fitness",
                "data": {
                    "work_event_id": "event-complete",
                    "body": "Fitness app delivered and verified.",
                },
            }
        )
        self.assertEqual(terminal["kind"], "completion")
        self.assertEqual(client.tasks["task-fitness"]["state"], "completed")
        self.assertEqual(client.tasks["task-fitness"]["timer_state"], "stopped")
        self.assertEqual(work_updates.active_task_id("carbon-a"), "")

        self.assertEqual(client.payloads("work_task_patch")[0]["revision"], 0)
        self.assertEqual(client.payloads("work_todo_patch")[0]["revision"], 0)
        self.assertEqual(client.payloads("work_blocker_resolve")[0]["revision"], 0)
        self.assertEqual(client.payloads("work_worker_group_patch")[0]["revision"], 0)
        self.assertEqual(client.payloads("work_worker_patch")[0]["revision"], 0)
        self.assertEqual(client.payloads("work_call_patch")[0]["revision"], 0)
        for name in (
            "work_task_create",
            "work_todo_add",
            "work_milestone_create",
            "work_blocker_create",
            "work_blocker_resolve",
            "work_worker_group_create",
            "work_worker_create",
            "work_call_create",
            "work_task_transition",
        ):
            self.assertTrue(client.payloads(name)[0].get("client_id"), name)

    def test_worker_lifecycle_and_transport_failures_are_best_effort(self):
        client = FakeWorkClient()
        self._create_task(client)

        first = work_updates.record_worker_started(
            "carbon-a",
            "worker-one",
            "browser",
            "Research the UI",
            queued=True,
            client=client,
        )
        second = work_updates.record_worker_started(
            "carbon-a",
            "worker-two",
            "terminal",
            "Build the UI",
            client=client,
        )
        self.assertEqual(len(client.payloads("work_worker_group_create")), 1)
        self.assertEqual(
            client.payloads("work_worker_create")[0]["state"],
            "yet_to_start",
        )
        self.assertEqual(first["group_id"], second["group_id"])
        self.assertTrue(
            work_updates.record_worker_state(
                "carbon-a",
                "worker-one",
                "in_progress",
                "Browser worker launched",
                client=client,
            )
        )
        self.assertTrue(
            work_updates.record_worker_state(
                "carbon-a",
                "worker-one",
                "completed",
                "Research delivered",
                client=client,
            )
        )
        patched_ids = [
            ids[-1]
            for name, ids, _payload in client.requests
            if name == "work_worker_patch"
        ]
        self.assertEqual(patched_ids, [first["invocation_id"], first["invocation_id"]])

        with mock.patch.object(work_updates, "get_contact", return_value=None):
            self.assertEqual(
                work_updates.record_worker_started(
                    "carbon-a",
                    "worker-no-room",
                    "terminal",
                    "Still launch the real worker",
                    task_id="task-fitness",
                    client=client,
                ),
                {},
            )
        self.assertFalse(
            work_updates.record_worker_state(
                "carbon-a",
                "worker-two",
                "failed",
                "Interface is offline",
                client=ExplodingClient(),
            )
        )
        result = work_updates.execute_work_update(
            {
                "action": "task/create",
                "data": {"title": "Will not publish"},
            },
            "carbon-a",
            client=ExplodingClient(),
        )
        self.assertIn("Error: work_update task/create failed", result)

    def test_failed_queued_browser_launch_marks_exact_worker_failed(self):
        from worker import handler

        queued = {
            "worker_id": "browser-one",
            "task": "Research references",
            "carbon_id": "carbon-a",
            "incognito": False,
            "providers": ["codex"],
        }
        with (
            mock.patch.object(handler, "_is_profiled_browser_active", return_value=False),
            mock.patch.object(handler, "_load_browser_queue", return_value=[queued]),
            mock.patch.object(handler, "_save_browser_queue") as save_queue,
            mock.patch.object(
                handler,
                "_launch_with_provider_order",
                return_value=(False, "all providers unavailable"),
            ),
            mock.patch.object(work_updates, "record_worker_state") as record_state,
        ):
            result, contact_id = handler._process_browser_queue()

        save_queue.assert_called_once_with([])
        record_state.assert_called_once_with(
            "carbon-a",
            "browser-one",
            "failed",
            "Browser worker failed to launch",
        )
        self.assertEqual(contact_id, "carbon-a")
        self.assertIn("Dequeued but failed to start", result)

    def test_call_bridge_creates_standalone_cards_and_mirrors_transcript(self):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="No active task yet.",
            client=client,
        )
        inbound = work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="No active task yet.",
            outbound=outbound,
            client=client,
        )
        continuation = work_updates.record_outbound_call(
            "carbon-b",
            target_kind="manager",
            target_id="carbon-a",
            target_name="Ada's manager",
            message="Received.",
            client=client,
        )

        self.assertEqual(outbound["task_id"], "")
        self.assertTrue(outbound["call_id"])
        self.assertTrue(outbound["work_event_id"])
        self.assertEqual(inbound["task_id"], "")
        self.assertTrue(inbound["call_id"])
        self.assertTrue(continuation["continuation"])
        self.assertEqual(continuation["call_id"], inbound["call_id"])
        outbound_event = client._event_by("", "call_id", outbound["call_id"])
        inbound_event = client._event_by("", "call_id", inbound["call_id"])
        self.assertEqual(outbound_event["room_id"], "room-a")
        self.assertEqual(inbound_event["room_id"], "room-b")
        self.assertEqual(
            [row["body"] for row in outbound_event["transcript"]],
            ["No active task yet.", "Received."],
        )
        self.assertEqual(
            [row["body"] for row in inbound_event["transcript"]],
            ["No active task yet.", "Received."],
        )
        self.assertEqual(outbound_event["state"], "completed")
        self.assertEqual(inbound_event["state"], "completed")
        state = work_updates._read_state()
        self.assertFalse(state["contacts"]["carbon-a"]["pending_calls"])
        self.assertFalse(state["contacts"]["carbon-b"]["pending_calls"])
        next_call = work_updates.prepare_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="A new topic.",
        )
        self.assertFalse(next_call.get("continuation", False))
        self.assertNotEqual(next_call["call_id"], outbound["call_id"])

    def test_same_side_followup_stays_open_until_opposite_side_responds(self):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Initial question.",
            client=client,
        )
        inbound = work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Initial question.",
            outbound=outbound,
            client=client,
        )
        previous_activity = (
            time.time() - work_updates.PENDING_CALL_TTL_SECONDS + 60
        )
        with work_updates._state_guard():
            state = work_updates._read_state()
            for contact in state["contacts"].values():
                for correlation in contact["pending_calls"].values():
                    correlation["updated_at"] = previous_activity
            work_updates._write_state(state)

        followup = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="One more detail.",
            client=client,
        )

        self.assertTrue(followup["continuation"])
        self.assertEqual(followup["continuation_role"], "outbound")
        outbound_event = client._event_by("", "call_id", outbound["call_id"])
        inbound_event = client._event_by("", "call_id", inbound["call_id"])
        self.assertEqual(outbound_event["state"], "in_progress")
        self.assertEqual(inbound_event["state"], "in_progress")
        self.assertEqual(
            [row["body"] for row in outbound_event["transcript"]],
            ["Initial question.", "One more detail."],
        )
        state = work_updates._read_state()
        self.assertGreater(
            state["contacts"]["carbon-a"]["pending_calls"]["carbon-b"][
                "updated_at"
            ],
            previous_activity,
        )

        response = work_updates.record_outbound_call(
            "carbon-b",
            target_kind="manager",
            target_id="carbon-a",
            target_name="Ada's manager",
            message="Confirmed.",
            client=client,
        )

        self.assertTrue(response["continuation"])
        self.assertEqual(response["continuation_role"], "inbound")
        self.assertEqual(outbound_event["state"], "completed")
        self.assertEqual(inbound_event["state"], "completed")
        self.assertEqual(
            [row["body"] for row in outbound_event["transcript"]],
            ["Initial question.", "One more detail.", "Confirmed."],
        )

    def test_call_idle_timeout_completes_mirrors_and_late_activity_is_new_call(
        self,
    ):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please confirm.",
            client=client,
        )
        inbound = work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Please confirm.",
            outbound=outbound,
            client=client,
        )
        last_activity = time.time()
        with work_updates._state_guard():
            state = work_updates._read_state()
            for contact in state["contacts"].values():
                for correlation in contact["pending_calls"].values():
                    correlation["updated_at"] = last_activity
            work_updates._write_state(state)

        self.assertEqual(
            work_updates.complete_inactive_calls(
                now=last_activity
                + work_updates.CALL_IDLE_TIMEOUT_SECONDS
                - 0.001,
                client=client,
            ),
            0,
        )
        self.assertEqual(
            client._event_by("", "call_id", outbound["call_id"])["state"],
            "in_progress",
        )
        self.assertEqual(
            work_updates.complete_inactive_calls(
                now=last_activity + work_updates.CALL_IDLE_TIMEOUT_SECONDS,
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())

        outbound_event = client._event_by("", "call_id", outbound["call_id"])
        inbound_event = client._event_by("", "call_id", inbound["call_id"])
        self.assertEqual(outbound_event["state"], "completed")
        self.assertEqual(inbound_event["state"], "completed")
        self.assertEqual(
            [row["body"] for row in outbound_event["transcript"]],
            ["Please confirm."],
        )
        state = work_updates._read_state()
        self.assertFalse(state["contacts"]["carbon-a"]["pending_calls"])
        self.assertFalse(state["contacts"]["carbon-b"]["pending_calls"])

        later = work_updates.prepare_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Following up later.",
        )
        self.assertFalse(later.get("continuation", False))
        self.assertNotEqual(later["call_id"], outbound["call_id"])

    def test_call_activity_resets_the_ten_second_idle_deadline(self):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Initial question.",
            client=client,
        )
        work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Initial question.",
            outbound=outbound,
            client=client,
        )
        old_activity = time.time() - 9.0
        with work_updates._state_guard():
            state = work_updates._read_state()
            for contact in state["contacts"].values():
                for correlation in contact["pending_calls"].values():
                    correlation["updated_at"] = old_activity
            work_updates._write_state(state)

        followup = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="One more detail.",
            client=client,
        )
        self.assertTrue(followup["continuation"])
        state = work_updates._read_state()
        refreshed = state["contacts"]["carbon-a"]["pending_calls"]["carbon-b"][
            "updated_at"
        ]
        self.assertGreater(refreshed, old_activity)
        self.assertEqual(
            work_updates.complete_inactive_calls(
                now=refreshed + work_updates.CALL_IDLE_TIMEOUT_SECONDS - 0.001,
                client=client,
            ),
            0,
        )
        self.assertEqual(
            work_updates.complete_inactive_calls(
                now=refreshed + work_updates.CALL_IDLE_TIMEOUT_SECONDS,
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())
        self.assertEqual(
            client._event_by("", "call_id", outbound["call_id"])["state"],
            "completed",
        )

    def test_manager_activity_refreshes_both_call_mirrors(self):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please investigate.",
            client=client,
        )
        work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Please investigate.",
            outbound=outbound,
            client=client,
        )
        old_activity = time.time() - 9.0
        refreshed_at = old_activity + 8.0
        with work_updates._state_guard():
            state = work_updates._read_state()
            for contact in state["contacts"].values():
                for correlation in contact["pending_calls"].values():
                    correlation["updated_at"] = old_activity
            work_updates._write_state(state)

        self.assertTrue(
            work_updates.touch_manager_call_activity(
                "carbon-b",
                now=refreshed_at,
            )
        )
        state = work_updates._read_state()
        for owner, peer in (
            ("carbon-a", "carbon-b"),
            ("carbon-b", "carbon-a"),
        ):
            self.assertEqual(
                state["contacts"][owner]["pending_calls"][peer]["updated_at"],
                refreshed_at,
            )
        self.assertEqual(
            work_updates.complete_inactive_calls(
                now=refreshed_at
                + work_updates.CALL_IDLE_TIMEOUT_SECONDS
                - 0.001,
                client=client,
            ),
            0,
        )

    def test_idle_timeout_recovers_visible_call_without_live_correlation(self):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="This correlation will be lost.",
            client=client,
        )
        last_activity = time.time()
        with work_updates._state_guard():
            state = work_updates._read_state()
            state["contacts"]["carbon-a"]["pending_calls"].clear()
            cached = state["contacts"]["carbon-a"]["standalone_calls"][
                outbound["call_id"]
            ]
            cached["_cached_at"] = last_activity
            work_updates._write_state(state)

        self.assertEqual(
            work_updates.complete_inactive_calls(
                now=last_activity + work_updates.CALL_IDLE_TIMEOUT_SECONDS,
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())
        self.assertEqual(
            client._event_by("", "call_id", outbound["call_id"])["state"],
            "completed",
        )
        self.assertEqual(work_updates.complete_inactive_calls(client=client), 0)

    def test_idle_completion_is_durable_and_never_reuses_the_old_call(self):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please check.",
            client=client,
        )
        inbound = work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Please check.",
            outbound=outbound,
            client=client,
        )
        last_activity = time.time() - work_updates.CALL_IDLE_TIMEOUT_SECONDS
        with work_updates._state_guard():
            state = work_updates._read_state()
            for contact in state["contacts"].values():
                for correlation in contact["pending_calls"].values():
                    correlation["updated_at"] = last_activity
            work_updates._write_state(state)

        with mock.patch.object(
            work_updates,
            "submit_best_effort",
            return_value=False,
        ):
            self.assertEqual(
                work_updates.complete_inactive_calls(client=client),
                1,
            )
            self.assertEqual(
                work_updates.complete_inactive_calls(client=client),
                0,
            )

        state = work_updates._read_state()
        pending_patches = [
            entry
            for entry in state["call_retry_journal"].values()
            if entry["operation"] == "patch"
        ]
        self.assertEqual(len(pending_patches), 2)
        self.assertTrue(
            state["contacts"]["carbon-a"]["pending_calls"]["carbon-b"][
                "terminal_requested"
            ]
        )
        later = work_updates.prepare_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Checking again later.",
        )
        self.assertFalse(later.get("continuation", False))
        self.assertNotEqual(later["call_id"], outbound["call_id"])

        self.assertEqual(
            work_updates.replay_pending_call_updates(
                now=float("inf"),
                client=client,
            ),
            2,
        )
        self.assertTrue(flush_best_effort())
        self.assertEqual(
            client._event_by("", "call_id", outbound["call_id"])["state"],
            "completed",
        )
        self.assertEqual(
            client._event_by("", "call_id", inbound["call_id"])["state"],
            "completed",
        )

    def test_prepared_continuation_after_idle_boundary_gets_a_fresh_call(self):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Original request.",
            client=client,
        )
        inbound = work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Original request.",
            outbound=outbound,
            client=client,
        )
        prepared_before_timeout = work_updates.prepare_outbound_call(
            "carbon-b",
            target_kind="manager",
            target_id="carbon-a",
            target_name="Ada's manager",
            message="Late response.",
        )
        self.assertTrue(prepared_before_timeout["continuation"])
        self.assertEqual(prepared_before_timeout["call_id"], inbound["call_id"])

        last_activity = time.time() - work_updates.CALL_IDLE_TIMEOUT_SECONDS
        with work_updates._state_guard():
            state = work_updates._read_state()
            for contact in state["contacts"].values():
                for correlation in contact["pending_calls"].values():
                    correlation["updated_at"] = last_activity
            work_updates._write_state(state)
        with mock.patch.object(
            work_updates,
            "submit_best_effort",
            return_value=False,
        ):
            self.assertEqual(work_updates.complete_inactive_calls(), 1)

        self.assertTrue(
            work_updates.enqueue_outbound_call(
                prepared_before_timeout,
                target_name="Ada's manager",
                message="Late response.",
                client=client,
                idempotency_key="late-manager-handoff",
            )
        )
        self.assertTrue(flush_best_effort())
        self.assertFalse(prepared_before_timeout.get("continuation", False))
        self.assertNotEqual(
            prepared_before_timeout["call_id"],
            inbound["call_id"],
        )
        self.assertEqual(
            client._event_by(
                "",
                "call_id",
                prepared_before_timeout["call_id"],
            )["state"],
            "in_progress",
        )

    def test_prepared_standalone_call_persists_after_async_enqueue(self):
        client = FakeWorkClient()
        reference = work_updates.prepare_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please review this.",
        )

        self.assertEqual(reference["task_id"], "")
        self.assertTrue(reference["call_id"])
        self.assertTrue(reference["work_event_id"])
        self.assertTrue(
            work_updates.enqueue_outbound_call(
                reference,
                target_name="Babbage's manager",
                message="Please review this.",
                client=client,
            )
        )
        state = work_updates._read_state()
        correlation = state["contacts"]["carbon-a"]["pending_calls"]["carbon-b"]
        self.assertEqual(correlation["outbound_call_id"], reference["call_id"])
        self.assertTrue(flush_best_effort())
        event = client._event_by("", "call_id", reference["call_id"])
        self.assertEqual(event["room_id"], "room-a")
        self.assertEqual(event["transcript"][0]["body"], "Please review this.")

    def test_completed_continuation_receipt_accepts_source_replay(self):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please confirm.",
            client=client,
        )
        inbound = work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Please confirm.",
            outbound=outbound,
            client=client,
        )
        continuation = work_updates.prepare_outbound_call(
            "carbon-b",
            target_kind="manager",
            target_id="carbon-a",
            target_name="Ada's manager",
            message="Confirmed.",
        )
        dedupe_key = "manager-handoff:queue-response:outbound"

        self.assertTrue(
            work_updates.enqueue_outbound_call(
                continuation,
                target_name="Ada's manager",
                message="Confirmed.",
                client=client,
                idempotency_key=dedupe_key,
            )
        )
        self.assertTrue(flush_best_effort())
        self.assertFalse(
            work_updates._read_state()["contacts"]["carbon-b"][
                "pending_calls"
            ]
        )
        request_count = len(client.requests)

        self.assertTrue(
            work_updates.enqueue_outbound_call(
                continuation,
                target_name="Ada's manager",
                message="Confirmed.",
                client=client,
                idempotency_key=dedupe_key,
            )
        )
        self.assertEqual(len(client.requests), request_count)
        self.assertTrue(continuation["continuation"])
        self.assertEqual(continuation["call_id"], inbound["call_id"])
        state = work_updates._read_state()
        self.assertFalse(state["contacts"]["carbon-a"]["pending_calls"])
        self.assertFalse(state["contacts"]["carbon-b"]["pending_calls"])

    def test_rejected_inbound_enqueue_keeps_durable_card_reference(self):
        outbound = {
            "owner_contact_id": "carbon-a",
            "task_id": "",
            "call_id": "call-outbound",
            "work_event_id": "event-outbound",
        }
        with mock.patch.object(
            work_updates,
            "submit_best_effort",
            return_value=False,
        ):
            inbound = work_updates.enqueue_inbound_call(
                "carbon-b",
                source_kind="manager",
                source_id="carbon-a",
                source_name="Ada's manager",
                message="Please review this.",
                outbound=outbound,
                client=FakeWorkClient(),
            )

        self.assertTrue(inbound["call_id"])
        self.assertTrue(inbound["work_event_id"])
        state = work_updates._read_state()
        self.assertEqual(len(state["call_retry_journal"]), 1)
        retry = next(iter(state["call_retry_journal"].values()))
        self.assertEqual(retry["direction"], "inbound")
        self.assertEqual(retry["reference"]["call_id"], inbound["call_id"])
        for owner, peer in (
            ("carbon-a", "carbon-b"),
            ("carbon-b", "carbon-a"),
        ):
            correlation = state["contacts"][owner]["pending_calls"][peer]
            self.assertEqual(
                correlation["outbound_call_id"],
                "call-outbound",
            )
            self.assertEqual(correlation["inbound_call_id"], inbound["call_id"])
            self.assertEqual(
                correlation["inbound_work_event_id"],
                inbound["work_event_id"],
            )

    def test_call_update_without_task_id_stays_standalone_after_cache_loss(self):
        client = FakeWorkClient()
        runtime = work_updates.WorkUpdates("carbon-a", client=client)
        runtime.execute(
            {
                "action": "call/create",
                "standalone": True,
                "data": {
                    "call_id": "call-standalone",
                    "work_event_id": "event-standalone",
                    "target_kind": "manager",
                    "target_id": "carbon-b",
                    "target_name": "Babbage's manager",
                    "state": "in_progress",
                },
            }
        )
        self._create_task(client, "carbon-a", "unrelated-active-task")
        state = work_updates._read_state()
        state["contacts"]["carbon-a"]["standalone_calls"] = {}
        work_updates._write_state(state)

        runtime.execute(
            {
                "action": "call/update",
                "call_id": "call-standalone",
                "data": {"state": "completed"},
            }
        )

        self.assertEqual(
            client.requests[-1][0:2],
            ("work_standalone_call_patch", ("call-standalone",)),
        )
        self.assertFalse(
            any(
                name == "work_call_patch"
                for name, _ids, _payload in client.requests
            )
        )

    def test_standalone_call_publication_failure_is_durably_retryable(self):
        reference = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="The real message must remain independent.",
            client=ExplodingClient(),
        )

        self.assertTrue(reference["call_id"])
        state = work_updates._read_state()
        self.assertTrue(state["contacts"]["carbon-a"]["pending_calls"])
        self.assertEqual(len(state["call_retry_journal"]), 1)
        retry = next(iter(state["call_retry_journal"].values()))
        self.assertEqual(retry["attempts"], 1)
        self.assertEqual(retry["last_error"], "OSError")
        self.assertEqual(
            work_updates.pending_call_update_retries(),
            {
                "pending": 1,
                "failed": 1,
                "dead_letter": 0,
                "total": 1,
                "archived_dead_letter": 0,
                "overflow_count": 0,
                "last_overflow_at": 0.0,
                "oldest_created_at": retry["created_at"],
                "next_attempt_at": retry["next_attempt_at"],
            },
        )

    def test_transient_retry_budget_covers_about_twenty_four_hours(self):
        nominal_delays = [
            min(
                work_updates.CALL_RETRY_BASE_DELAY_SECONDS
                * (2 ** min(attempt - 1, 12)),
                work_updates.CALL_RETRY_MAX_DELAY_SECONDS,
            )
            for attempt in range(1, work_updates.CALL_RETRY_MAX_ATTEMPTS)
        ]
        nominal_horizon = sum(nominal_delays)

        self.assertGreaterEqual(nominal_horizon, 24 * 60 * 60)
        self.assertLess(nominal_horizon, 25 * 60 * 60)

    def test_auth_failure_remains_retryable_and_recovers_after_rotation(self):
        client = FakeWorkClient()
        reference = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please confirm.",
            client=client,
        )
        retry_id = work_updates._journal_call_patch(
            reference,
            {"body": "Authentication recovered."},
            mutation_id="auth-recovery",
        )
        successful_patch = client.work_standalone_call_patch
        attempts = 0

        def rotate_then_patch(call_id, payload):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise interface.WorkCallMutationError(
                    status_code=401,
                    code="silicon_key_rejected",
                    retryable=True,
                )
            return successful_patch(call_id, payload)

        client.work_standalone_call_patch = rotate_then_patch
        self.assertFalse(
            work_updates._deliver_call_retry(retry_id, client=client)
        )
        health = work_updates.pending_call_update_retries()
        self.assertEqual(health["pending"], 1)
        self.assertEqual(health["dead_letter"], 0)

        self.assertEqual(
            work_updates.replay_pending_call_updates(
                now=float("inf"),
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())
        self.assertEqual(work_updates.pending_call_update_retries()["pending"], 0)
        event = client._event_by("", "call_id", reference["call_id"])
        self.assertEqual(event["body"], "Authentication recovered.")

    def test_permanent_create_failure_uses_cli_api_status_and_dead_letters(self):
        class InvalidCreateClient:
            def work_standalone_call_create(self, _payload):
                raise interface.InterfaceError(
                    'api 400: {"code":"invalid_call","detail":"omitted"}'
                )

        work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please confirm.",
            client=InvalidCreateClient(),
        )

        state = work_updates._read_state()
        entry = next(iter(state["call_retry_journal"].values()))
        self.assertEqual(entry["status"], "dead_letter")
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(entry["last_error"], "InterfaceError:http_400")
        health = work_updates.pending_call_update_retries()
        self.assertEqual(health["pending"], 0)
        self.assertEqual(health["dead_letter"], 1)

    def test_retry_diagnostics_never_store_or_print_cli_payloads(self):
        secret = "TOP-SECRET TRANSCRIPT"

        class SensitiveFailureClient:
            def work_standalone_call_create(self, _payload):
                raise interface.InterfaceError(
                    f"command si work call create --data {secret}"
                )

        with mock.patch("builtins.print") as printed:
            work_updates.record_outbound_call(
                "carbon-a",
                target_kind="manager",
                target_id="carbon-b",
                target_name="Babbage's manager",
                message=secret,
                client=SensitiveFailureClient(),
            )

        state = work_updates._read_state()
        entry = next(iter(state["call_retry_journal"].values()))
        self.assertEqual(entry["last_error"], "InterfaceError")
        rendered_logs = " ".join(
            str(value)
            for call in printed.call_args_list
            for value in call.args
        )
        self.assertNotIn(secret, rendered_logs)
        self.assertNotIn("--data", rendered_logs)

    def test_retry_journal_cap_fails_atomically_and_reports_overflow(self):
        first = {
            "owner_contact_id": "carbon-a",
            "call_id": "call-first",
        }
        second = {
            "owner_contact_id": "carbon-a",
            "call_id": "call-second",
        }
        with mock.patch.object(
            work_updates,
            "CALL_RETRY_MAX_ENTRIES",
            1,
        ):
            work_updates._journal_call_patch(
                first,
                {"body": "first"},
                mutation_id="first",
            )
            with self.assertRaises(work_updates.WorkUpdateError):
                work_updates._journal_call_patch(
                    second,
                    {"body": "second"},
                    mutation_id="second",
                )

        health = work_updates.pending_call_update_retries()
        self.assertEqual(health["total"], 1)
        self.assertEqual(health["overflow_count"], 1)
        state = work_updates._read_state()
        self.assertEqual(
            {
                entry["reference"]["call_id"]
                for entry in state["call_retry_journal"].values()
            },
            {"call-first"},
        )

    def test_retry_journal_cap_archives_oldest_failed_entry_for_new_ingress(self):
        first = {
            "owner_contact_id": "carbon-a",
            "call_id": "call-first",
        }
        second = {
            "owner_contact_id": "carbon-a",
            "call_id": "call-second",
        }
        with mock.patch.object(
            work_updates,
            "CALL_RETRY_MAX_ENTRIES",
            1,
        ):
            first_id = work_updates._journal_call_patch(
                first,
                {"body": "private first body"},
                mutation_id="first",
            )
            with work_updates._state_guard():
                state = work_updates._read_state()
                state["call_retry_journal"][first_id].update(
                    {
                        "attempts": 3,
                        "last_error": "WorkCallMutationError:http_500",
                        "next_attempt_at": time.time() + 300,
                    }
                )
                work_updates._write_state(state)

            work_updates._journal_call_patch(
                second,
                {"body": "second"},
                mutation_id="second",
            )

        state = work_updates._read_state()
        self.assertEqual(
            {
                entry["reference"]["call_id"]
                for entry in state["call_retry_journal"].values()
            },
            {"call-second"},
        )
        self.assertEqual(state["call_retry_overflow_count"], 1)
        self.assertEqual(len(state["call_retry_dead_letters"]), 1)
        archived = state["call_retry_dead_letters"][0]
        self.assertEqual(archived["retry_id"], first_id)
        self.assertIn("capacity_evicted", archived["last_error"])
        self.assertNotIn(
            "private first body",
            json.dumps(state["call_retry_dead_letters"]),
        )

    def test_expired_dead_letter_archival_is_body_free(self):
        secret = "PRIVATE CALL BODY"
        retry_id = work_updates._journal_call_patch(
            {
                "owner_contact_id": "carbon-a",
                "call_id": "call-private",
            },
            {"body": secret},
            mutation_id="private",
        )
        with work_updates._state_guard():
            state = work_updates._read_state()
            entry = state["call_retry_journal"][retry_id]
            entry["status"] = "dead_letter"
            entry["attempts"] = work_updates.CALL_RETRY_MAX_ATTEMPTS
            entry["last_error"] = "InterfaceError:http_422"
            entry["dead_lettered_at"] = (
                time.time()
                - work_updates.CALL_RETRY_DEAD_LETTER_RETENTION_SECONDS
                - 1
            )
            work_updates._write_state(state)

        health = work_updates.pending_call_update_retries()
        self.assertEqual(health["total"], 0)
        self.assertEqual(health["archived_dead_letter"], 1)
        persisted = work_updates.WORK_UPDATES_FILE.read_text(
            encoding="utf-8"
        )
        self.assertNotIn(secret, persisted)

    def test_failed_call_create_replays_with_stable_idempotent_payload(self):
        client = FakeWorkClient()
        successful_create = client.work_standalone_call_create
        create = mock.Mock()

        def flaky_create(payload):
            if create.call_count == 1:
                successful_create(payload)
                raise OSError("response lost after Glass committed")
            return successful_create(payload)

        create.side_effect = flaky_create
        client.work_standalone_call_create = create

        reference = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please review this.",
            client=client,
        )

        self.assertEqual(work_updates.pending_call_update_retries()["pending"], 1)
        self.assertEqual(create.call_count, 1)
        scheduled = work_updates.replay_pending_call_updates(
            now=float("inf"),
            client=client,
        )
        self.assertEqual(scheduled, 1)
        self.assertTrue(flush_best_effort())
        self.assertEqual(create.call_count, 2)
        first_payload = create.call_args_list[0].args[0]
        second_payload = create.call_args_list[1].args[0]
        self.assertEqual(first_payload, second_payload)
        self.assertTrue(first_payload["client_id"])
        self.assertEqual(
            first_payload["transcript"][0]["transcript_id"],
            second_payload["transcript"][0]["transcript_id"],
        )
        self.assertEqual(
            first_payload["transcript"][0]["created_at"],
            second_payload["transcript"][0]["created_at"],
        )
        self.assertEqual(
            first_payload["transcript"][0]["updated_at"],
            second_payload["transcript"][0]["updated_at"],
        )
        self.assertEqual(work_updates.pending_call_update_retries()["pending"], 0)
        event = client._event_by("", "call_id", reference["call_id"])
        self.assertEqual(event["transcript"][0]["body"], "Please review this.")

    def test_failed_inbound_call_replays_without_losing_correlation(self):
        reference = work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Can you review this?",
            outbound={
                "owner_contact_id": "carbon-a",
                "task_id": "",
                "call_id": "call-outbound",
                "work_event_id": "event-outbound",
            },
            client=ExplodingClient(),
        )
        state = work_updates._read_state()
        self.assertEqual(len(state["call_retry_journal"]), 1)
        self.assertEqual(
            state["contacts"]["carbon-b"]["pending_calls"]["carbon-a"][
                "inbound_call_id"
            ],
            reference["call_id"],
        )

        client = FakeWorkClient()
        self.assertEqual(
            work_updates.replay_pending_call_updates(
                now=float("inf"),
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())
        self.assertEqual(work_updates.pending_call_update_retries()["pending"], 0)
        event = client._event_by("", "call_id", reference["call_id"])
        self.assertEqual(event["direction"], "inbound")
        self.assertEqual(event["transcript"][0]["body"], "Can you review this?")

    def test_pending_call_create_replays_later_transcript_and_terminal_state(self):
        reference = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Initial question.",
            client=ExplodingClient(),
        )
        continuation = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Additional context.",
            client=ExplodingClient(),
        )
        queued = work_updates.WorkUpdates(
            "carbon-a",
            client=ExplodingClient(),
        ).execute(
            {
                "action": "call/update",
                "call_id": reference["call_id"],
                "standalone": True,
                "data": {
                    "state": "completed",
                    "body": "Babbage replied.",
                },
            }
        )

        self.assertTrue(continuation["continuation"])
        self.assertTrue(queued["queued_for_delivery"])
        journal = sorted(
            work_updates._read_state()["call_retry_journal"].values(),
            key=lambda entry: entry["sequence"],
        )
        self.assertEqual(
            [entry["operation"] for entry in journal],
            ["create", "patch", "patch"],
        )
        terminal = journal[-1]["payload"]
        self.assertEqual(terminal["state"], "completed")
        self.assertEqual(terminal["body"], "Babbage replied.")
        self.assertEqual(
            [
                row["body"]
                for row in journal[1]["payload"]["transcript"]
            ],
            ["Additional context."],
        )

        client = FakeWorkClient()
        self.assertEqual(
            work_updates.replay_pending_call_updates(
                now=float("inf"),
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())
        self.assertEqual(work_updates.pending_call_update_retries()["pending"], 0)
        event = client._event_by("", "call_id", reference["call_id"])
        self.assertEqual(event["state"], "completed")
        self.assertEqual(event["body"], "Babbage replied.")
        self.assertEqual(
            [row["body"] for row in event["transcript"]],
            ["Initial question.", "Additional context."],
        )
        self.assertFalse(
            work_updates._read_state()["contacts"]["carbon-a"]["pending_calls"]
        )

    def test_call_journal_preserves_update_added_during_create(self):
        client = FakeWorkClient()
        original_create = client.work_standalone_call_create
        reference_box = {}

        def racing_create(payload):
            if not reference_box:
                reference_box.update(
                    owner_contact_id="carbon-a",
                    call_id=payload["call_id"],
                )
                work_updates.WorkUpdates("carbon-a", client=client).execute(
                    {
                        "action": "call/update",
                        "call_id": payload["call_id"],
                        "standalone": True,
                        "data": {"state": "completed"},
                    }
                )
            return original_create(payload)

        client.work_standalone_call_create = racing_create
        with mock.patch.object(
            work_updates,
            "_schedule_next_call_lane",
            return_value=False,
        ):
            reference = work_updates.record_outbound_call(
                "carbon-a",
                target_kind="manager",
                target_id="carbon-b",
                target_name="Babbage's manager",
                message="Please confirm.",
                client=client,
            )

        journal = work_updates._read_state()["call_retry_journal"]
        self.assertEqual(len(journal), 1)
        self.assertEqual(
            next(iter(journal.values()))["payload"]["state"],
            "completed",
        )
        self.assertEqual(
            work_updates.replay_pending_call_updates(
                now=float("inf"),
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())
        self.assertEqual(work_updates.pending_call_update_retries()["pending"], 0)
        self.assertEqual(
            client._event_by("", "call_id", reference["call_id"])["state"],
            "completed",
        )

    def test_expired_call_lease_is_taken_over_after_ninety_seconds(self):
        reference = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please confirm.",
            client=ExplodingClient(),
        )
        with work_updates._state_guard():
            state = work_updates._read_state()
            entry = next(iter(state["call_retry_journal"].values()))
            entry["next_attempt_at"] = 0.0
            entry["lease_owner"] = "old-process"
            entry["lease_token"] = "old-token"
            entry["lease_expires_at"] = 1_090.0
            work_updates._write_state(state)

        client = FakeWorkClient()
        self.assertEqual(
            work_updates.replay_pending_call_updates(
                now=1_089.9,
                client=client,
            ),
            0,
        )
        self.assertEqual(
            work_updates.replay_pending_call_updates(
                now=1_090.1,
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())
        self.assertEqual(work_updates.pending_call_update_retries()["pending"], 0)
        self.assertEqual(
            client._event_by("", "call_id", reference["call_id"])[
                "transcript"
            ][0]["body"],
            "Please confirm.",
        )

    def test_lost_patch_response_replays_without_duplicate_transcript(self):
        client = FakeWorkClient()
        reference = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Initial question.",
            client=client,
        )
        successful_patch = client.work_standalone_call_patch
        attempts = 0

        def flaky_patch(call_id, payload):
            nonlocal attempts
            attempts += 1
            event = client._event_by("", "call_id", call_id)
            if attempts == 1:
                successful_patch(call_id, payload)
                raise OSError("response lost after Glass committed")
            if payload.get("revision") != event["revision"]:
                raise interface.WorkCallMutationError(
                    status_code=409,
                    code="revision_conflict",
                    current_revision=event["revision"],
                    retryable=True,
                )
            return successful_patch(call_id, payload)

        client.work_standalone_call_patch = flaky_patch
        self.assertTrue(
            work_updates.record_contact_call_message(
                "carbon-a",
                peer_contact_id="carbon-b",
                speaker_kind="manager",
                speaker_id="manager:carbon-a",
                speaker_name="Ada's manager",
                message="Additional context.",
                client=client,
                idempotency_key="event-outgoing-1",
            )
        )
        self.assertTrue(flush_best_effort())
        self.assertEqual(work_updates.pending_call_update_retries()["pending"], 1)

        self.assertEqual(
            work_updates.replay_pending_call_updates(
                now=float("inf"),
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())
        event = client._event_by("", "call_id", reference["call_id"])
        self.assertEqual(
            [row["body"] for row in event["transcript"]],
            ["Initial question.", "Additional context."],
        )
        self.assertEqual(
            len({row["transcript_id"] for row in event["transcript"]}),
            2,
        )
        self.assertEqual(attempts, 3)

    def test_dead_patch_does_not_block_later_terminal_mutation(self):
        client = FakeWorkClient()
        reference = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Initial question.",
            client=client,
        )
        retry_id = work_updates._journal_call_patch(
            reference,
            {"body": "A malformed optional update."},
            mutation_id="malformed-update",
        )
        original_patch = client.work_standalone_call_patch
        client.work_standalone_call_patch = mock.Mock(
            side_effect=ValueError("invalid optional patch")
        )
        self.assertFalse(
            work_updates._deliver_call_retry(retry_id, client=client)
        )
        client.work_standalone_call_patch = original_patch
        self.assertEqual(
            work_updates.pending_call_update_retries()["dead_letter"],
            1,
        )

        result = work_updates.WorkUpdates("carbon-a", client=client).execute(
            {
                "action": "call/update",
                "call_id": reference["call_id"],
                "standalone": True,
                "data": {
                    "state": "completed",
                    "body": "The call completed.",
                },
            }
        )

        self.assertFalse(result.get("queued_for_delivery", False))
        self.assertEqual(
            client._event_by("", "call_id", reference["call_id"])["state"],
            "completed",
        )

    def test_inbound_event_id_replay_keeps_one_canonical_card(self):
        outbound = {
            "owner_contact_id": "carbon-a",
            "call_id": "call-outbound",
            "work_event_id": "event-outbound",
        }
        with mock.patch.object(
            work_updates,
            "_schedule_call_retry",
            return_value=True,
        ):
            first = work_updates.enqueue_inbound_call(
                "carbon-b",
                source_kind="manager",
                source_id="carbon-a",
                source_name="Ada's manager",
                message="Please review.",
                outbound=outbound,
                idempotency_key="incoming-event-1",
            )
            second = work_updates.enqueue_inbound_call(
                "carbon-b",
                source_kind="manager",
                source_id="carbon-a",
                source_name="Ada's manager",
                message="Please review.",
                outbound=outbound,
                idempotency_key="incoming-event-1",
            )

        self.assertEqual(first, second)
        state = work_updates._read_state()
        self.assertEqual(len(state["call_retry_journal"]), 1)
        receipt = state["call_retry_dedupe"]["incoming-event-1"]
        self.assertNotIn(
            "Please review.",
            json.dumps(receipt, sort_keys=True),
        )
        self.assertEqual(
            state["contacts"]["carbon-b"]["pending_calls"]["carbon-a"][
                "inbound_call_id"
            ],
            first["call_id"],
        )

    def test_initial_outgoing_self_echo_does_not_duplicate_or_close_call(self):
        self.contacts["silicon-b"] = {
            "contact_type": "silicon",
            "silicon_id": "silicon-b",
            "room_id": "room-b",
            "display_name": "Babbage",
        }
        dedupe_key = "outgoing-call:silicon-b:event-outgoing-1"
        reference = work_updates.prepare_outbound_call(
            "silicon-b",
            target_kind="silicon",
            target_id="silicon-b",
            target_name="Babbage",
            message="Hello.",
        )
        with mock.patch.object(
            work_updates,
            "_schedule_call_retry",
            return_value=True,
        ) as schedule:
            self.assertTrue(
                work_updates.enqueue_outbound_call(
                    reference,
                    target_name="Babbage",
                    message="Hello.",
                    idempotency_key=dedupe_key,
                )
            )
            self.assertTrue(
                work_updates.record_contact_call_message(
                    "silicon-b",
                    speaker_kind="manager",
                    speaker_id="silicon-self",
                    speaker_name="Silicon",
                    message="Hello.",
                    idempotency_key=dedupe_key,
                    terminal=True,
                )
            )

        state = work_updates._read_state()
        journal = list(state["call_retry_journal"].values())
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]["operation"], "create")
        correlation = state["contacts"]["silicon-b"]["pending_calls"][
            "silicon-b"
        ]
        self.assertFalse(correlation.get("terminal_requested", False))
        self.assertEqual(schedule.call_count, 2)

    def test_direct_silicon_bookkeeping_uses_only_its_peer_correlation(self):
        self.contacts["silicon-b"] = {
            "contact_type": "silicon",
            "silicon_id": "silicon-b",
            "room_id": "room-b",
            "display_name": "Babbage",
        }
        client = FakeWorkClient()
        direct = work_updates.record_outbound_call(
            "silicon-b",
            target_kind="silicon",
            target_id="silicon-b",
            target_name="Babbage",
            message="Direct question.",
            client=client,
        )
        unrelated = work_updates.record_outbound_call(
            "silicon-b",
            target_kind="silicon",
            target_id="silicon-c",
            target_name="Curie",
            message="Unrelated question.",
            client=client,
        )
        with work_updates._state_guard():
            state = work_updates._read_state()
            pending = state["contacts"]["silicon-b"]["pending_calls"]
            pending["silicon-b"]["updated_at"] = time.time() - 2.0
            pending["silicon-c"]["updated_at"] = time.time() - 1.0
            work_updates._write_state(state)

        self.assertTrue(
            work_updates.record_contact_call_message(
                "silicon-b",
                speaker_kind="silicon",
                speaker_id="silicon-b",
                speaker_name="Babbage",
                message="Direct answer.",
                client=client,
                idempotency_key="incoming-call:silicon-b:event-direct",
                terminal=True,
            )
        )
        self.assertTrue(flush_best_effort())

        direct_event = client._event_by("", "call_id", direct["call_id"])
        unrelated_event = client._event_by("", "call_id", unrelated["call_id"])
        self.assertEqual(direct_event["state"], "completed")
        self.assertEqual(
            [row["body"] for row in direct_event["transcript"]],
            ["Direct question.", "Direct answer."],
        )
        self.assertEqual(unrelated_event["state"], "in_progress")
        self.assertEqual(
            [row["body"] for row in unrelated_event["transcript"]],
            ["Unrelated question."],
        )
        pending = work_updates._read_state()["contacts"]["silicon-b"][
            "pending_calls"
        ]
        self.assertNotIn("silicon-b", pending)
        self.assertIn("silicon-c", pending)

    def test_completed_terminal_append_receipt_replay_is_accepted_noop(self):
        self.contacts["silicon-b"] = {
            "contact_type": "silicon",
            "silicon_id": "silicon-b",
            "room_id": "room-b",
            "display_name": "Babbage",
        }
        client = FakeWorkClient()
        reference = work_updates.record_outbound_call(
            "silicon-b",
            target_kind="silicon",
            target_id="silicon-b",
            target_name="Babbage",
            message="Please confirm.",
            client=client,
        )
        dedupe_key = "incoming-call:silicon-b:event-response"
        self.assertTrue(
            work_updates.record_contact_call_message(
                "silicon-b",
                speaker_kind="silicon",
                speaker_id="silicon-b",
                speaker_name="Babbage",
                message="Confirmed.",
                client=client,
                idempotency_key=dedupe_key,
                terminal=True,
            )
        )
        self.assertTrue(flush_best_effort())
        self.assertFalse(
            work_updates._read_state()["contacts"]["silicon-b"]["pending_calls"]
        )
        create_count = len(client.payloads("work_standalone_call_create"))
        patch_count = len(client.payloads("work_standalone_call_patch"))

        replayed = work_updates.record_contact_call_message(
            "silicon-b",
            speaker_kind="silicon",
            speaker_id="silicon-b",
            speaker_name="Babbage",
            message="Confirmed.",
            client=client,
            idempotency_key=dedupe_key,
            terminal=True,
        )
        if not replayed:
            work_updates.enqueue_inbound_call(
                "silicon-b",
                source_kind="silicon",
                source_id="silicon-b",
                source_name="Babbage",
                message="Confirmed.",
                client=client,
                idempotency_key=dedupe_key,
            )
        self.assertTrue(flush_best_effort())

        self.assertTrue(replayed)
        self.assertEqual(
            len(client.payloads("work_standalone_call_create")),
            create_count,
        )
        self.assertEqual(
            len(client.payloads("work_standalone_call_patch")),
            patch_count,
        )
        self.assertFalse(
            work_updates._read_state()["contacts"]["silicon-b"]["pending_calls"]
        )
        event = client._event_by("", "call_id", reference["call_id"])
        self.assertEqual(
            [row["body"] for row in event["transcript"]],
            ["Please confirm.", "Confirmed."],
        )

    def test_task_linked_call_bridge_still_mirrors_transcript(self):
        client = FakeWorkClient()

        self._create_task(client, "carbon-a", "task-a")
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Can you confirm the colour?",
            client=client,
        )
        self._create_task(client, "carbon-b", "task-b")
        inbound = work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Can you confirm the colour?",
            outbound=outbound,
            client=client,
        )
        continuation = work_updates.record_outbound_call(
            "carbon-b",
            target_kind="manager",
            target_id="carbon-a",
            target_name="Ada's manager",
            message="Use blue.",
            client=client,
        )

        self.assertTrue(continuation["continuation"])
        self.assertEqual(continuation["owner_contact_id"], "carbon-b")
        self.assertEqual(continuation["task_id"], "task-b")
        self.assertEqual(continuation["call_id"], inbound["call_id"])
        outbound_event = client._event_by("task-a", "call_id", outbound["call_id"])
        inbound_event = client._event_by("task-b", "call_id", inbound["call_id"])
        self.assertEqual(
            [row["body"] for row in outbound_event["transcript"]],
            ["Can you confirm the colour?", "Use blue."],
        )
        self.assertEqual(
            [row["body"] for row in inbound_event["transcript"]],
            ["Can you confirm the colour?", "Use blue."],
        )
        self.assertEqual(outbound_event["state"], "completed")
        self.assertEqual(inbound_event["state"], "completed")
        state = work_updates._read_state()
        self.assertFalse(state["contacts"]["carbon-a"]["pending_calls"])
        self.assertFalse(state["contacts"]["carbon-b"]["pending_calls"])

    def test_partial_mirror_failure_retries_only_the_missing_side(self):
        client = FakeWorkClient()
        outbound = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="Please confirm.",
            client=client,
        )
        inbound = work_updates.record_inbound_call(
            "carbon-b",
            source_kind="manager",
            source_id="carbon-a",
            source_name="Ada's manager",
            message="Please confirm.",
            outbound=outbound,
            client=client,
        )
        original_patch = client.work_standalone_call_patch

        def one_side_offline(call_id, payload):
            if call_id == outbound["call_id"]:
                raise OSError("outbound room offline")
            return original_patch(call_id, payload)

        client.work_standalone_call_patch = one_side_offline
        continuation = work_updates.record_outbound_call(
            "carbon-b",
            target_kind="manager",
            target_id="carbon-a",
            target_name="Ada's manager",
            message="Confirmed.",
            client=client,
        )

        self.assertTrue(continuation["continuation"])
        inbound_event = client._event_by("", "call_id", inbound["call_id"])
        self.assertEqual(inbound_event["state"], "completed")
        self.assertEqual(
            work_updates.pending_call_update_retries()["pending"],
            1,
        )
        creates_before_retry = len(
            client.payloads("work_standalone_call_create")
        )

        client.work_standalone_call_patch = original_patch
        self.assertEqual(
            work_updates.replay_pending_call_updates(
                now=float("inf"),
                client=client,
            ),
            1,
        )
        self.assertTrue(flush_best_effort())
        outbound_event = client._event_by("", "call_id", outbound["call_id"])
        self.assertEqual(outbound_event["state"], "completed")
        self.assertEqual(
            len(client.payloads("work_standalone_call_create")),
            creates_before_retry,
        )
        self.assertEqual(
            [row["body"] for row in outbound_event["transcript"]],
            ["Please confirm.", "Confirmed."],
        )

    def test_standalone_call_outer_event_can_be_correlated_for_replies(self):
        interface.ingest._remember_work_event_reference(
            {
                "event_id": "outer-call-event",
                "room_id": "room-a",
                "type": "m.work_event",
                "content": {
                    "task_id": None,
                    "kind": "call",
                    "work_event_id": "standalone-call-event",
                    "call_id": "standalone-call",
                },
            }
        )

        self.assertEqual(
            interface.ingest._work_event_reference("room-a", "outer-call-event"),
            {
                "kind": "call",
                "work_event_id": "standalone-call-event",
                "call_id": "standalone-call",
            },
        )

    def test_reply_to_outer_blocker_event_reaches_manager_context(self):
        state = interface.state._default_contacts_state()
        state["own_ids"] = ["silicon-self"]
        state["rooms"]["room-a"] = "carbon-a"
        state["contacts"]["carbon-a"] = {
            **self.contacts["carbon-a"],
            "last_processed_event_ids": [],
            "last_processed_event_id": "",
            "last_polled_event_id": "",
        }
        interface.state._save_state(state)
        client = FakeWorkClient()
        blocker_event = {
            "event_id": "outer-event-blocker",
            "room_id": "room-a",
            "type": "m.work_event",
            "sender_id": "silicon-self",
            "content": {
                "task_id": "task-fitness",
                "kind": "blocker",
                "work_event_id": "event-colour",
                "blocker_id": "blocker-colour",
                "body": "Should the primary colour be red or blue?",
            },
        }
        reply_event = {
            "event_id": "reply-event",
            "room_id": "room-a",
            "type": "m.text",
            "sender_id": "carbon-a",
            "content": {
                "body": "Use blue.",
                "reply_to_event_id": "outer-event-blocker",
            },
        }
        with (
            mock.patch("diagnostics.store.Diagnostics.get_active_run", return_value=None),
            mock.patch("diagnostics.store.Diagnostics.start_run", side_effect=RuntimeError),
            mock.patch("diagnostics.activity.incoming"),
        ):
            self.assertIsNone(interface.process_incoming_event(blocker_event, client))
            contact_id, context = interface.process_incoming_event(reply_event, client)
            self.assertTrue(flush_best_effort())

        self.assertEqual(contact_id, "carbon-a")
        expected = {
            "blocker_id": "blocker-colour",
            "kind": "blocker",
            "task_id": "task-fitness",
            "work_event_id": "event-colour",
        }
        self.assertIn(
            "reply_to_work_update:"
            + " "
            + json.dumps(
                expected,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            context,
        )
        self.assertIn("message:\nUse blue.", context)
        self.assertEqual(client.reads, [("room-a", "reply-event")])


if __name__ == "__main__":
    unittest.main()
