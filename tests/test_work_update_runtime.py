import json
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import core.interface as interface
import core.work_updates as work_updates
from core.background import flush_best_effort


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
        self.old_contacts = interface.CONTACTS_FILE
        self.old_backup = interface.CONTACTS_BACKUP_FILE
        self.old_legacy = interface.LEGACY_TELEGRAM_CONTACTS_FILE
        work_updates.WORK_UPDATES_FILE = root / "work_updates.json"
        interface.CONTACTS_FILE = root / "contacts.json"
        interface.CONTACTS_BACKUP_FILE = root / "contacts-backup.json"
        interface.LEGACY_TELEGRAM_CONTACTS_FILE = root / "legacy-contacts.json"
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
        interface.CONTACTS_FILE = self.old_contacts
        interface.CONTACTS_BACKUP_FILE = self.old_backup
        interface.LEGACY_TELEGRAM_CONTACTS_FILE = self.old_legacy
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
                interface,
                "get_contact",
                return_value=self.contacts["carbon-a"],
            ),
            mock.patch.object(
                interface,
                "InterfaceClient",
                return_value=rejected_client,
            ),
            mock.patch(
                "core.diagnostics.Diagnostics.get_active_run",
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
        state = work_updates._read_state()
        self.assertTrue(state["contacts"]["carbon-a"]["pending_calls"])
        self.assertTrue(state["contacts"]["carbon-b"]["pending_calls"])

        work_updates.WorkUpdates("carbon-a", client=client).execute(
            {
                "action": "call/update",
                "call_id": outbound["call_id"],
                "data": {"state": "completed"},
            }
        )
        self.assertEqual(outbound_event["state"], "completed")
        state = work_updates._read_state()
        self.assertFalse(state["contacts"]["carbon-a"]["pending_calls"])
        self.assertFalse(state["contacts"]["carbon-b"]["pending_calls"])

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

    def test_rejected_inbound_enqueue_discards_unpublished_card_reference(self):
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

        self.assertEqual(inbound["call_id"], "")
        self.assertEqual(inbound["work_event_id"], "")
        state = work_updates._read_state()
        for owner, peer in (
            ("carbon-a", "carbon-b"),
            ("carbon-b", "carbon-a"),
        ):
            correlation = state["contacts"][owner]["pending_calls"][peer]
            self.assertEqual(
                correlation["outbound_call_id"],
                "call-outbound",
            )
            self.assertEqual(correlation["inbound_call_id"], "")
            self.assertEqual(correlation["inbound_work_event_id"], "")

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

    def test_standalone_call_publication_failure_is_isolated(self):
        reference = work_updates.record_outbound_call(
            "carbon-a",
            target_kind="manager",
            target_id="carbon-b",
            target_name="Babbage's manager",
            message="The real message must remain independent.",
            client=ExplodingClient(),
        )

        self.assertEqual(reference, {})
        state = work_updates._read_state()
        self.assertFalse(state["contacts"]["carbon-a"]["pending_calls"])

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

    def test_standalone_call_outer_event_can_be_correlated_for_replies(self):
        interface._remember_work_event_reference(
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
            interface._work_event_reference("room-a", "outer-call-event"),
            {
                "kind": "call",
                "work_event_id": "standalone-call-event",
                "call_id": "standalone-call",
            },
        )

    def test_reply_to_outer_blocker_event_reaches_manager_context(self):
        state = interface._default_contacts_state()
        state["own_ids"] = ["silicon-self"]
        state["rooms"]["room-a"] = "carbon-a"
        state["contacts"]["carbon-a"] = {
            **self.contacts["carbon-a"],
            "last_processed_event_ids": [],
            "last_processed_event_id": "",
            "last_polled_event_id": "",
        }
        interface._save_state(state)
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
            mock.patch("core.diagnostics.Diagnostics.get_active_run", return_value=None),
            mock.patch("core.diagnostics.Diagnostics.start_run", side_effect=RuntimeError),
            mock.patch("core.activity_log.incoming"),
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
