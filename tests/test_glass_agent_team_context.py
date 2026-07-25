import sys
import threading
import time
import types
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glass_agent


class _FakeWebSocket:
    def __init__(self, running=None):
        self.running = running
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)

    def recv(self, timeout=None):
        if self.running is not None:
            self.running[0] = False
        raise TimeoutError


class _ConnectContext:
    def __init__(self, websocket):
        self.websocket = websocket

    def __enter__(self):
        return self.websocket

    def __exit__(self, exc_type, exc, traceback):
        return False


class _RecordingReconciler:
    def __init__(self):
        self.requests = []

    def request(self, *, force=False, reason=""):
        self.requests.append((force, reason))


class _FailingReconciler:
    def request(self, *, force=False, reason=""):
        raise RuntimeError("thread unavailable")


def _websockets_modules(connect):
    package = types.ModuleType("websockets")
    package.__path__ = []
    sync_package = types.ModuleType("websockets.sync")
    sync_package.__path__ = []
    client = types.ModuleType("websockets.sync.client")
    client.connect = connect
    return {
        "websockets": package,
        "websockets.sync": sync_package,
        "websockets.sync.client": client,
    }


class TeamContextReconcilerTests(unittest.TestCase):
    def test_scheduling_failure_does_not_escape_websocket_handler(self):
        with mock.patch("builtins.print") as print_message:
            glass_agent.handle_message(
                _FakeWebSocket(),
                {"type": "team_context.changed", "kind": "metadata"},
                Path("/tmp/silicon"),
                "Test Silicon",
                team_context_reconciler=_FailingReconciler(),
            )

        self.assertIn("scheduling deferred", print_message.call_args.args[0])

    def test_invalidation_uses_conditional_reconciliation(self):
        reconciler = _RecordingReconciler()

        glass_agent.handle_message(
            _FakeWebSocket(),
            {"type": "team_context.changed", "kind": "advertising_memory"},
            Path("/tmp/silicon"),
            "Test Silicon",
            team_context_reconciler=reconciler,
        )

        self.assertEqual(
            reconciler.requests,
            [(False, "websocket-invalidation:advertising_memory")],
        )

    def test_requests_are_backgrounded_and_coalesced(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        calls = []

        def reconcile(root, force=False, reason=""):
            calls.append((Path(root), force, reason))
            if len(calls) == 1:
                started.set()
                release.wait(timeout=2)
            if len(calls) == 2:
                finished.set()

        module = types.ModuleType("core.team_context")
        module.reconcile_team_context = reconcile
        reconciler = glass_agent.TeamContextReconciler(Path("/tmp/silicon"))
        try:
            with mock.patch.dict(sys.modules, {"core.team_context": module}):
                reconciler.request(reason="websocket-connect")
                self.assertTrue(started.wait(timeout=1))

                reconciler.request(reason="websocket-safety")
                reconciler.request(force=True, reason="websocket-invalidation:metadata")
                release.set()

                self.assertTrue(finished.wait(timeout=1))
        finally:
            release.set()
            reconciler.stop()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1:], (False, "websocket-connect"))
        self.assertTrue(calls[1][1])
        self.assertIn("websocket-invalidation:metadata", calls[1][2])
        self.assertIn("websocket-safety", calls[1][2])

    def test_invalidation_handler_does_not_wait_for_reconciliation(self):
        started = threading.Event()
        release = threading.Event()

        def reconcile(_root, force=False, reason=""):
            started.set()
            release.wait(timeout=2)

        module = types.ModuleType("core.team_context")
        module.reconcile_team_context = reconcile
        reconciler = glass_agent.TeamContextReconciler(Path("/tmp/silicon"))
        try:
            with mock.patch.dict(sys.modules, {"core.team_context": module}):
                glass_agent.handle_message(
                    _FakeWebSocket(),
                    {"type": "team_context.changed", "kind": "advertising_memory"},
                    Path("/tmp/silicon"),
                    "Test Silicon",
                    team_context_reconciler=reconciler,
                )
                self.assertTrue(started.wait(timeout=1))
        finally:
            release.set()
            reconciler.stop()

    def test_reconciliation_error_is_fail_open_for_later_requests(self):
        second_call = threading.Event()
        calls = []

        def reconcile(_root, force=False, reason=""):
            calls.append(reason)
            if len(calls) == 1:
                raise RuntimeError("temporary outage")
            second_call.set()

        module = types.ModuleType("core.team_context")
        module.reconcile_team_context = reconcile
        reconciler = glass_agent.TeamContextReconciler(Path("/tmp/silicon"))
        try:
            with mock.patch.dict(sys.modules, {"core.team_context": module}):
                with mock.patch("builtins.print"):
                    reconciler.request(force=True, reason="first")
                    deadline = time.monotonic() + 1
                    while len(calls) < 1 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    reconciler.request(reason="second")
                    self.assertTrue(second_call.wait(timeout=1))
        finally:
            reconciler.stop()

        self.assertEqual(calls, ["first", "second"])


class GlassAgentLiveConnectionTests(unittest.TestCase):
    def test_retry_wait_wakes_for_rotated_alias_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.test",
                        "silicon_api_key": "rotated-key",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(glass_agent.time, "sleep") as sleep:
                glass_agent.wait_for_retry(
                    root,
                    [True],
                    delay=300,
                    rejected_key="old-key",
                )

        sleep.assert_not_called()

    def test_retry_wait_does_not_skip_normal_network_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".glass.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://glass.test",
                        "api_key": "current-key",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    glass_agent.time,
                    "monotonic",
                    side_effect=[0, 0, 0, 1],
                ),
                mock.patch.object(glass_agent.time, "sleep") as sleep,
            ):
                glass_agent.wait_for_retry(root, [True], delay=1)

        sleep.assert_called_once()

    def test_retry_wait_wakes_when_missing_server_url_is_added(self):
        root = Path("/tmp/silicon")
        with (
            mock.patch.object(
                glass_agent,
                "load_config",
                side_effect=[
                    {"api_key": "current-key"},
                    {
                        "server_url": "https://glass.test",
                        "api_key": "current-key",
                    },
                ],
            ),
            mock.patch.object(
                glass_agent.time,
                "monotonic",
                side_effect=[0, 0, 0, 0],
            ),
            mock.patch.object(glass_agent.time, "sleep") as sleep,
        ):
            glass_agent.wait_for_retry(
                root,
                [True],
                delay=300,
                rejected_key="current-key",
                rejected_server_url="",
            )

        sleep.assert_called_once()

    def test_loopback_ws_omits_ssl_and_runs_connect_and_safety_reconciliation(self):
        running = [True]
        websocket = _FakeWebSocket(running)
        connect_calls = []
        reconciler = _RecordingReconciler()
        connected = []

        def connect(url, **kwargs):
            connect_calls.append((url, kwargs))
            return _ConnectContext(websocket)

        with mock.patch.dict(sys.modules, _websockets_modules(connect)), mock.patch.object(
            glass_agent.time,
            "monotonic",
            side_effect=[0, 61],
        ), mock.patch.object(
            glass_agent.time,
            "time",
            return_value=123,
        ), mock.patch.object(
            glass_agent,
            "drain_diagnostics",
            return_value=0,
        ), mock.patch.object(
            glass_agent,
            "terminal_stop",
        ):
            glass_agent.run_live(
                Path("/tmp/silicon"),
                {
                    "server_url": "http://127.0.0.1:8000",
                    "silicon_api_key": "alias-key",
                },
                running,
                team_context_reconciler=reconciler,
                on_connected=lambda: connected.append(True),
            )

        self.assertEqual(
            connect_calls[0][0],
            "ws://127.0.0.1:8000/ws/glass/agent/",
        )
        self.assertNotIn("ssl", connect_calls[0][1])
        self.assertEqual(
            connect_calls[0][1]["additional_headers"],
            {"X-Silicon-Key": "alias-key"},
        )
        self.assertEqual(connected, [True])
        self.assertEqual(
            reconciler.requests,
            [
                (True, "websocket-connect"),
                (False, "websocket-safety"),
            ],
        )

    def test_remote_plaintext_ws_is_rejected_before_key_is_attached(self):
        connect = mock.Mock()

        with mock.patch.dict(sys.modules, _websockets_modules(connect)):
            with self.assertRaisesRegex(RuntimeError, "unsafe Glass WebSocket URL"):
                glass_agent.run_live(
                    Path("/tmp/silicon"),
                    {
                        "server_url": "http://glass.test",
                        "api_key": "must-not-be-sent",
                    },
                    [False],
                    team_context_reconciler=_RecordingReconciler(),
                )

        connect.assert_not_called()

    def test_loopback_websocket_hosts_are_allowed(self):
        expected = {
            "http://localhost:8000": "ws://localhost:8000/ws/glass/agent/",
            "http://localhost.:8000": "ws://localhost.:8000/ws/glass/agent/",
            "http://127.0.0.2:8000": "ws://127.0.0.2:8000/ws/glass/agent/",
            "http://[::1]:8000": "ws://[::1]:8000/ws/glass/agent/",
        }

        for server_url, websocket_url in expected.items():
            with self.subTest(server_url=server_url):
                self.assertEqual(glass_agent.ws_url(server_url), websocket_url)

    def test_native_websocket_schemes_are_rejected_as_server_urls(self):
        for server_url in ("ws://localhost:8000", "wss://glass.test"):
            with self.subTest(server_url=server_url):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "unsafe Glass WebSocket URL",
                ):
                    glass_agent.ws_url(server_url)

    def test_lookalike_and_obscured_websocket_destinations_are_rejected(self):
        unsafe_urls = (
            "http://localhost.example:8000",
            "http://glass.test",
            "ws://glass.test",
            "https://user:password@glass.test",
            "https://glass.test?redirect=http://localhost",
            "https://glass.test#localhost",
        )

        for server_url in unsafe_urls:
            with self.subTest(server_url=server_url):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "unsafe Glass WebSocket URL",
                ):
                    glass_agent.ws_url(server_url)

    def test_secure_ws_supplies_ssl_context(self):
        running = [False]
        websocket = _FakeWebSocket()
        connect_calls = []
        reconciler = _RecordingReconciler()
        ssl_sentinel = object()

        def connect(url, **kwargs):
            connect_calls.append((url, kwargs))
            return _ConnectContext(websocket)

        with mock.patch.dict(sys.modules, _websockets_modules(connect)), mock.patch.object(
            glass_agent,
            "ssl_context",
            return_value=ssl_sentinel,
        ), mock.patch.object(
            glass_agent,
            "drain_diagnostics",
            return_value=0,
        ), mock.patch.object(
            glass_agent,
            "terminal_stop",
        ):
            glass_agent.run_live(
                Path("/tmp/silicon"),
                {"server_url": "https://glass.test", "api_key": "primary-key"},
                running,
                team_context_reconciler=reconciler,
            )

        self.assertEqual(connect_calls[0][0], "wss://glass.test/ws/glass/agent/")
        self.assertIs(connect_calls[0][1]["ssl"], ssl_sentinel)


class SidecarPlatformParityTests(unittest.TestCase):
    def test_missing_pty_disables_only_interactive_terminal(self):
        websocket = _FakeWebSocket()
        with mock.patch.object(glass_agent, "pty", None), mock.patch.object(
            glass_agent.shutil,
            "which",
            return_value="codex",
        ), mock.patch.object(
            glass_agent,
            "terminal_stop",
        ):
            glass_agent.terminal_start(
                websocket,
                Path("/tmp/silicon"),
                "codex",
            )

        self.assertEqual(len(websocket.sent), 1)
        self.assertIn("not supported on this platform", websocket.sent[0])

if __name__ == "__main__":
    unittest.main()
