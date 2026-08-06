"""Subprocess entrypoint for a Codex app-server backed worker.

Runs one task to completion against ``codex app-server`` and prints the result
for ``worker.handler`` to collect.  Transport lives in
``core.codex_app_server``; this module only adds the worker presentation.
"""
import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.codex_app_server import CodexAppServer as _SharedCodexAppServer
from core.progress import codex_progress_event


CODEX_CMD = shutil.which("codex") or shutil.which("codex.cmd") or "codex"


class CodexAppServer(_SharedCodexAppServer):
    """Worker output hooks around the shared app-server transport."""

    def __init__(self, cwd):
        self.progress_state = {}
        super().__init__(cwd, command=CODEX_CMD, timeout=60)

    def _emit(self, payload):
        print(json.dumps(payload, separators=(",", ":")), flush=True)

    def _handle_stdout_line(self, line):
        if not line:
            return
        print(line, flush=True)
        try:
            msg = json.loads(line)
            progress = codex_progress_event(msg, self.progress_state)
            if progress:
                print(
                    json.dumps(
                        progress,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
        except (json.JSONDecodeError, ValueError):
            pass

    def _handle_stderr_line(self, line):
        if line:
            self._emit({"type": "codex.stderr", "message": line})


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--system-prompt-file", required=True)
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    task = sys.stdin.read()
    system_prompt = read_file(args.system_prompt_file)
    client = CodexAppServer(args.cwd)

    try:
        client.request_result(
            "initialize",
            {
                "clientInfo": {"name": "silicon-worker", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
            timeout=30,
        )

        thread_params = {
            "cwd": args.cwd,
            "baseInstructions": system_prompt,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if args.thread_id:
            thread_result = client.request_result(
                "thread/resume",
                {**thread_params, "threadId": args.thread_id},
                timeout=60,
            )
        else:
            thread_result = client.request_result(
                "thread/start",
                {**thread_params, "ephemeral": False},
                timeout=60,
            )
        thread = thread_result["thread"]
        client._emit({
            "type": "silicon.codex_context",
            "model": thread_result.get("model", ""),
            "model_provider": thread_result.get("modelProvider", ""),
        })

        client.request_result(
            "turn/start",
            {
                "threadId": thread["id"],
                "cwd": args.cwd,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
                "input": [{"type": "text", "text": task}],
            },
            timeout=60,
        )
        client.run_until_turn_completed(args.timeout)
    except Exception as e:
        print(json.dumps({"type": "silicon.codex_app_error", "message": str(e)}), flush=True)
        return 1
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
