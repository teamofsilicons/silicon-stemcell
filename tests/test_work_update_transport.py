import unittest
from unittest import mock

from core.interface import InterfaceClient, InterfaceError


class FakeGlassResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        return self.payload


class WorkUpdateTransportTest(unittest.TestCase):
    def setUp(self):
        self.client = InterfaceClient()
        self.run = mock.Mock(return_value={"ok": True})
        self.client.run = self.run

    def test_send_appends_progress_group_and_work_continues_flags(self):
        self.client.send(
            "room-1",
            "Still working",
            progress_group_id="run-1",
            work_continues=True,
        )

        self.run.assert_called_once_with(
            [
                "send",
                "room-1",
                "Still working",
                "--group",
                "run-1",
                "--work-continues",
            ],
            timeout=60,
        )

    def test_send_omits_unset_work_update_flags(self):
        self.client.send("room-1", "Finished")

        self.run.assert_called_once_with(
            ["send", "room-1", "Finished"],
            timeout=60,
        )

    def test_progress_emits_frame_metadata_and_zero_values(self):
        self.client.progress(
            "room-1",
            "run-1",
            "spawning_worker",
            "Starting a worker",
            "frame-1",
            task_id="task-1",
            revision=0,
            occurred_at="2026-07-23T08:20:00Z",
            progress_pct=0,
            summary="Worker launch",
        )

        self.run.assert_called_once_with(
            [
                "progress",
                "room-1",
                "spawning_worker",
                "--group",
                "run-1",
                "--note",
                "Starting a worker",
                "--frame",
                "frame-1",
                "--task",
                "task-1",
                "--revision",
                "0",
                "--at",
                "2026-07-23T08:20:00Z",
                "--pct",
                "0",
                "--summary",
                "Worker launch",
            ],
            timeout=30,
        )

    def test_progress_requires_a_frame_and_omits_unset_metadata(self):
        with self.assertRaises(TypeError):
            self.client.progress("room-1", "run-1", "thinking", "")

        self.client.progress("room-1", "run-1", "thinking", "", "frame-1")

        self.run.assert_called_once_with(
            [
                "progress",
                "room-1",
                "thinking",
                "--group",
                "run-1",
                "--frame",
                "frame-1",
            ],
            timeout=30,
        )

    def test_work_task_read_commands_build_exact_argv(self):
        self.client.work_task_list(
            "room-1",
            state="running",
            cursor="next page",
            limit=25,
        )
        self.client.work_task_list()
        self.client.work_task_show("task-1")

        self.assertEqual(
            self.run.call_args_list,
            [
                mock.call(
                    [
                        "work",
                        "task",
                        "list",
                        "room-1",
                        "--state",
                        "running",
                        "--cursor",
                        "next page",
                        "--limit",
                        "25",
                    ],
                    timeout=60,
                ),
                mock.call(["work", "task", "list"], timeout=60),
                mock.call(
                    ["work", "task", "show", "task-1"],
                    timeout=60,
                ),
            ],
        )

    def test_work_mutations_build_exact_argv_with_compact_json(self):
        payload = {
            "client_id": "caller-owned-id",
            "body": "Working",
            "nested": {"count": 1},
        }
        data = (
            '{"client_id":"caller-owned-id","body":"Working",'
            '"nested":{"count":1}}'
        )

        self.client.work_task_create(payload)
        self.client.work_task_patch("task-1", payload)
        self.client.work_todo_add("task-1", payload)
        self.client.work_todo_patch("task-1", "todo-1", payload)
        self.client.work_milestone_create("task-1", payload)
        self.client.work_blocker_create("task-1", payload)
        self.client.work_blocker_resolve("task-1", "blocker-1", payload)
        self.client.work_worker_group_create("task-1", payload)
        self.client.work_worker_group_patch("task-1", "group-1", payload)
        self.client.work_worker_create("task-1", "group-1", payload)
        self.client.work_worker_patch(
            "task-1",
            "group-1",
            "invocation-1",
            payload,
        )
        self.client.work_call_create("task-1", payload)
        self.client.work_call_patch("task-1", "call-1", payload)

        expected_prefixes = [
            ["work", "task", "create"],
            ["work", "task", "patch", "task-1"],
            ["work", "todo", "add", "task-1"],
            ["work", "todo", "patch", "task-1", "todo-1"],
            ["work", "milestone", "update", "task-1"],
            ["work", "blocker", "create", "task-1"],
            ["work", "blocker", "resolve", "task-1", "blocker-1"],
            ["work", "worker-group", "create", "task-1"],
            ["work", "worker-group", "patch", "task-1", "group-1"],
            ["work", "worker", "create", "task-1", "group-1"],
            [
                "work",
                "worker",
                "patch",
                "task-1",
                "group-1",
                "invocation-1",
            ],
            ["work", "call", "create", "task-1"],
            ["work", "call", "patch", "task-1", "call-1"],
        ]
        self.assertEqual(
            self.run.call_args_list,
            [
                mock.call([*prefix, "--data", data], timeout=60)
                for prefix in expected_prefixes
            ],
        )
        self.assertEqual(payload["client_id"], "caller-owned-id")

    @mock.patch("core.glass.silicon_api_request")
    def test_standalone_call_mutations_use_authenticated_glass_transport(
        self,
        request,
    ):
        request.side_effect = [
            FakeGlassResponse(201, {"call_id": "call:standalone"}),
            FakeGlassResponse(
                200,
                {"call_id": "call:standalone", "state": "completed"},
            ),
        ]
        create_payload = {
            "call_id": "call:standalone",
            "state": "connecting",
            "client_id": "create-call:standalone",
        }
        patch_payload = {"state": "completed", "revision": 0}

        created = self.client.work_standalone_call_create(create_payload)
        patched = self.client.work_standalone_call_patch(
            "call:standalone",
            patch_payload,
        )

        self.assertEqual(created, {"call_id": "call:standalone"})
        self.assertEqual(
            patched,
            {"call_id": "call:standalone", "state": "completed"},
        )
        self.assertEqual(
            request.call_args_list,
            [
                mock.call(
                    "POST",
                    "/api/v1/work/calls",
                    json_body=create_payload,
                    timeout=60,
                ),
                mock.call(
                    "PATCH",
                    "/api/v1/work/calls/call%3Astandalone",
                    json_body=patch_payload,
                    timeout=60,
                ),
            ],
        )
        self.run.assert_not_called()

    @mock.patch("core.glass.silicon_api_request")
    def test_standalone_call_mutation_surfaces_glass_error_detail(self, request):
        request.return_value = FakeGlassResponse(
            404,
            {"detail": "Room is unavailable."},
        )

        with self.assertRaisesRegex(InterfaceError, "Room is unavailable"):
            self.client.work_standalone_call_create({"room_id": "missing"})

        self.run.assert_not_called()

    @mock.patch("core.interface.time.sleep")
    @mock.patch("core.glass.silicon_api_request")
    def test_standalone_call_create_retries_transient_glass_failure(
        self,
        request,
        sleep,
    ):
        request.side_effect = [
            FakeGlassResponse(
                503,
                {"detail": "Temporarily unavailable."},
                {"Retry-After": "0"},
            ),
            FakeGlassResponse(201, {"call_id": "call-standalone"}),
        ]
        payload = {
            "room_id": "room-1",
            "client_id": "create-call-standalone",
        }

        result = self.client.work_standalone_call_create(payload)

        self.assertEqual(result, {"call_id": "call-standalone"})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.0)
        self.run.assert_not_called()

    def test_task_transition_wrappers_build_exact_argv(self):
        payload = {"client_id": "transition-id", "body": "Finished"}
        data = '{"client_id":"transition-id","body":"Finished"}'

        self.client.work_task_transition("task-1", "complete", payload)
        self.client.work_task_complete("task-2", payload)
        self.client.work_task_fail("task-3", payload)
        self.client.work_task_cancel("task-4", payload)

        self.assertEqual(
            self.run.call_args_list,
            [
                mock.call(
                    ["work", "complete", "task-1", "--data", data],
                    timeout=60,
                ),
                mock.call(
                    ["work", "complete", "task-2", "--data", data],
                    timeout=60,
                ),
                mock.call(
                    ["work", "fail", "task-3", "--data", data],
                    timeout=60,
                ),
                mock.call(
                    ["work", "cancel", "task-4", "--data", data],
                    timeout=60,
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
