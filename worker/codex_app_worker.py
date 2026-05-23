import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time


CODEX_CMD = shutil.which("codex") or shutil.which("codex.cmd") or "codex"


class CodexAppServer:
    def __init__(self, cwd):
        self.cwd = cwd
        self.next_id = 1
        self.messages = queue.Queue()
        self.proc = subprocess.Popen(
            [
                CODEX_CMD,
                "app-server",
                "--listen",
                "stdio://",
                "--config",
                'sandbox_mode="danger-full-access"',
                "--config",
                'approval_policy="never"',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _emit(self, payload):
        print(json.dumps(payload, separators=(",", ":")), flush=True)

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line:
                print(line, flush=True)
                self.messages.put(("stdout", line))

    def _read_stderr(self):
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            if line:
                self._emit({"type": "codex.stderr", "message": line})
                self.messages.put(("stderr", line))

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def send(self, method, params=None):
        msg_id = self.next_id
        self.next_id += 1
        payload = {"id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        return msg_id

    def respond(self, msg_id, result):
        self.proc.stdin.write(json.dumps({"id": msg_id, "result": result}) + "\n")
        self.proc.stdin.flush()

    def _handle_server_request(self, msg):
        method = msg.get("method", "")
        msg_id = msg.get("id")
        if msg_id is None:
            return False

        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            self.respond(msg_id, {"decision": "acceptForSession"})
            return True
        if method == "item/tool/requestUserInput":
            self.respond(msg_id, {"canceled": True})
            return True
        if method == "mcpServer/elicitation/request":
            self.respond(msg_id, {"action": "cancel"})
            return True
        return False

    def request(self, method, params=None, timeout=60):
        req_id = self.send(method, params)
        deadline = time.time() + timeout

        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"codex app-server exited with code {self.proc.returncode}")
            try:
                source, line = self.messages.get(timeout=0.25)
            except queue.Empty:
                continue
            if source != "stdout":
                continue
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if self._handle_server_request(msg):
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", f"{method} failed"))
                return msg.get("result", {})

        raise subprocess.TimeoutExpired([CODEX_CMD, "app-server", method], timeout)

    def run_until_turn_completed(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"codex app-server exited with code {self.proc.returncode}")
            try:
                source, line = self.messages.get(timeout=0.25)
            except queue.Empty:
                continue
            if source != "stdout":
                continue
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if self._handle_server_request(msg):
                continue
            if msg.get("method") == "turn/completed":
                turn = msg.get("params", {}).get("turn", {})
                if turn.get("status") == "failed":
                    err = turn.get("error") or {}
                    raise RuntimeError(err.get("message", "Codex turn failed"))
                return
        raise subprocess.TimeoutExpired([CODEX_CMD, "app-server", "turn"], timeout)


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
        client.request(
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
            thread = client.request(
                "thread/resume",
                {**thread_params, "threadId": args.thread_id},
                timeout=60,
            )["thread"]
        else:
            thread = client.request(
                "thread/start",
                {**thread_params, "ephemeral": False},
                timeout=60,
            )["thread"]

        client.request(
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
