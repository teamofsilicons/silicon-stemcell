"""Shared JSON-RPC transport for ``codex app-server``.

Manager and worker callers have different presentation needs, but process
lifecycle, approvals, request matching, and timeout behavior must stay
identical.  Subclasses may override the two line hooks without reimplementing
the transport.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any


class CodexAppServer:
    """Small, thread-backed JSON-RPC client for Codex app-server stdio."""

    def __init__(
        self,
        cwd: str,
        *,
        command: str = "codex",
        timeout: float = 180,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.cwd = cwd
        self.command = command
        self.timeout = timeout
        self.next_id = 1
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.proc = popen_factory(
            [
                command,
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

    def _handle_stdout_line(self, _line: str) -> None:
        """Presentation hook called before a stdout line is queued."""

    def _handle_stderr_line(self, _line: str) -> None:
        """Presentation hook called before a stderr line is queued."""

    def _read_stdout(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self._handle_stdout_line(line)
            self.messages.put(("stdout", line))

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            self.stderr_lines.append(line)
            self._handle_stderr_line(line)
            self.messages.put(("stderr", line))

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        msg_id: int | None = None,
    ) -> int:
        if msg_id is None:
            msg_id = self.next_id
            self.next_id += 1
        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        return msg_id

    def respond(self, msg_id: int, result: dict[str, Any]) -> None:
        self.proc.stdin.write(json.dumps({"id": msg_id, "result": result}) + "\n")
        self.proc.stdin.flush()

    def handle_server_request(self, message: dict[str, Any]) -> bool:
        """Answer all non-interactive approval/elicitation requests."""

        method = message.get("method", "")
        msg_id = message.get("id")
        if msg_id is None:
            return False
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            self.respond(msg_id, {"decision": "acceptForSession"})
            return True
        if method == "item/tool/requestUserInput":
            self.respond(msg_id, {"canceled": True})
            return True
        if method == "mcpServer/elicitation/request":
            self.respond(msg_id, {"action": "cancel"})
            return True
        return False

    # Compatibility for callers that historically used the private spelling.
    _handle_server_request = handle_server_request

    def _next_message(self, deadline: float) -> tuple[str, dict[str, Any]] | None:
        while time.time() < deadline:
            try:
                source, line = self.messages.get(timeout=0.25)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(self.process_exit_message())
                continue
            if source == "stderr":
                continue
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            if self.handle_server_request(message):
                continue
            return source, message
        return None

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Return the complete JSON-RPC response for one request."""

        req_id = self.send(method, params)
        wait_for = self.timeout if timeout is None else timeout
        deadline = time.time() + wait_for
        while time.time() < deadline:
            item = self._next_message(deadline)
            if item is None:
                break
            _source, message = item
            if message.get("id") == req_id:
                return message
        raise subprocess.TimeoutExpired(
            [self.command, "app-server", method],
            wait_for,
        )

    def request_result(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Return a successful result or raise the server's error."""

        message = self.request(method, params, timeout)
        if "error" in message:
            error = message.get("error") or {}
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(detail or f"{method} failed")
        result = message.get("result", {})
        return result if isinstance(result, dict) else {}

    def run_until_turn_completed(self, timeout: float) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self._next_message(deadline)
            if item is None:
                break
            _source, message = item
            if message.get("method") != "turn/completed":
                continue
            turn = message.get("params", {}).get("turn", {})
            if turn.get("status") == "failed":
                error = turn.get("error") or {}
                detail = error.get("message") if isinstance(error, dict) else str(error)
                raise RuntimeError(detail or "Codex turn failed")
            return turn
        raise subprocess.TimeoutExpired(
            [self.command, "app-server", "turn"],
            timeout,
        )

    def process_exit_message(self) -> str:
        detail = "\n".join(self.stderr_lines[-5:]).strip()
        message = f"codex app-server exited with code {self.proc.returncode}"
        return message + (f": {detail}" if detail else "")

    # Compatibility for existing manager call sites.
    _process_exit_message = process_exit_message
