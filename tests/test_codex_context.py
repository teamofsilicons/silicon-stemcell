import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inference.codex.app_server import CodexAppServer, TracedAppServer
from inference.codex.provider import CodexProvider
from worker.codex_app_worker import CodexAppServer as WorkerCodexAppServer


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, params, timeout):
        self.calls.append((method, params, timeout))
        return self.response


class TestCodexContextCapture(unittest.TestCase):
    def test_manager_and_worker_share_one_app_server_transport(self):
        self.assertTrue(issubclass(TracedAppServer, CodexAppServer))
        self.assertTrue(issubclass(WorkerCodexAppServer, CodexAppServer))

    def test_raw_stream_log_omits_all_fragmented_agent_message_text(self):
        secret = "team-visible advertising payload"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.jsonl"
            client = object.__new__(TracedAppServer)
            client.stream_log_path = str(path)
            client._write_stream_log(
                json.dumps(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "itemId": "item-1",
                            "delta": f'"content":"{secret}",',
                        },
                    }
                )
            )
            client._write_stream_log(
                json.dumps(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "itemId": "item-1",
                            "delta": '"tool":"advertising_memory/update"',
                        },
                    }
                )
            )
            client._write_stream_log(
                json.dumps(
                    {
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "id": "item-1",
                                "type": "agentMessage",
                                "text": secret,
                            }
                        },
                    }
                )
            )

            logged = path.read_text(encoding="utf-8")

        self.assertNotIn(secret, logged)
        self.assertNotIn("advertising_memory/update", logged)
        self.assertIn('"redacted":true', logged)

    def test_raw_stream_log_omits_provider_stderr(self):
        secret = "ADVERTISING_PAYLOAD_SHOULD_NOT_BE_DURABLE"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.jsonl"
            client = object.__new__(TracedAppServer)
            client.stream_log_path = str(path)
            client._write_stream_log(
                json.dumps(
                    {
                        "type": "codex.stderr",
                        "message": secret,
                    }
                )
            )

            logged = path.read_text(encoding="utf-8")

        self.assertNotIn(secret, logged)
        self.assertIn('"redacted":true', logged)

    def test_raw_stream_log_omits_provider_error_payloads(self):
        secret = (
            '{"tools":[{"tool":"advertising_memory/update",'
            '"content":"PRIVATE_ERROR_ECHO"}]}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stream.jsonl"
            client = object.__new__(TracedAppServer)
            client.stream_log_path = str(path)
            client._write_stream_log(
                json.dumps({
                    "method": "error",
                    "params": {"error": {"message": secret}},
                })
            )
            client._write_stream_log(secret)

            logged = path.read_text(encoding="utf-8")

        self.assertNotIn("PRIVATE_ERROR_ECHO", logged)
        self.assertIn('"redacted":true', logged)

    def test_new_thread_returns_model_context(self):
        provider = CodexProvider()
        client = FakeClient({"result": {
            "thread": {"id": "thread-new"},
            "model": "gpt-5.6-sol",
            "modelProvider": "openai",
        }})
        with (
            patch.object(provider.sessions, "read", return_value=""),
            patch.object(provider.sessions, "write") as write,
        ):
            thread_id, context = provider.start_or_resume_thread(
                client, "carbon-1", "system prompt"
            )
        self.assertEqual(thread_id, "thread-new")
        self.assertEqual(context, {
            "model": "gpt-5.6-sol", "model_provider": "openai",
        })
        self.assertEqual(client.calls[0][0], "thread/start")
        write.assert_called_once_with("carbon-1", "thread-new")

    def test_resumed_thread_returns_model_context(self):
        provider = CodexProvider()
        client = FakeClient({"result": {
            "thread": {"id": "thread-old"},
            "model": "gpt-5.6-terra",
            "modelProvider": "openai",
        }})
        with patch.object(provider.sessions, "read", return_value="thread-old"):
            thread_id, context = provider.start_or_resume_thread(
                client, "carbon-1", "system prompt"
            )
        self.assertEqual(thread_id, "thread-old")
        self.assertEqual(context["model"], "gpt-5.6-terra")
        self.assertEqual(client.calls[0][0], "thread/resume")


if __name__ == "__main__":
    unittest.main()
