"""Talking to the Interface CLI: the socket, the fallback, the parsing.

The CLI is the protocol adapter. This layer prefers its RPC socket, falls back
to spawning it, and turns whatever comes back into JSON. Nothing here knows
what a contact or a work card is.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from interface.constants import PROJECT_ROOT, RPC_MAX_RESPONSE_BYTES
from interface.errors import InterfaceError, _RPCUnavailable, _RPCUnsupported


def _as_list(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _parse_json_output(stdout: str) -> Any:
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
    return {"text": text}



class Transport:
    """Small adapter around ``si --json``.

    Command methods intentionally keep a thin shape. The CLI is the protocol
    adapter; Stemcell only normalizes JSON and builds stable calls.
    """


    def __init__(self, executable: str | None = None, cwd: Path | None = None):
        self.executable = executable
        self.cwd = Path(cwd or PROJECT_ROOT)

    def _candidates(self) -> list[str]:
        if self.executable:
            return [self.executable]
        local = self.cwd / ".silicon-interface" / "bin" / "si"
        return [str(local), "si", "silicon-interface"]

    def _resolve_executable(self) -> str:
        for candidate in self._candidates():
            if os.path.sep in candidate:
                if Path(candidate).exists():
                    return candidate
            elif shutil.which(candidate):
                return candidate
        raise InterfaceError("Silicon Interface CLI not found. Expected ./.silicon-interface/bin/si, si, or silicon-interface.")

    def base_cmd(self, json_mode: bool = True) -> list[str]:
        cmd = [self._resolve_executable()]
        if json_mode:
            cmd.append("--json")
        return cmd

    def rpc_socket_path(self) -> Path:
        configured = str(
            os.environ.get("SILICON_INTERFACE_RPC_SOCKET") or ""
        ).strip()
        if configured:
            return Path(configured).expanduser()
        root = Path(
            os.environ.get("SILICON_INTERFACE_ROOT") or self.cwd
        ).expanduser().resolve()
        state_dir = root / ".silicon-interface"
        discovery = state_dir / "daemon-rpc.json"
        try:
            value = json.loads(discovery.read_text(encoding="utf-8"))
            socket_value = str(value.get("socket") or "")
            if value.get("version") == 1 and socket_value:
                candidate = Path(socket_value).expanduser()
                if candidate.is_absolute():
                    return candidate
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return state_dir / "daemon.sock"

    def _run_rpc(self, args: list[str], *, timeout: int, check: bool) -> Any:
        request_id = secrets.token_hex(16)
        request = json.dumps(
            {
                "version": 1,
                "id": request_id,
                "command": str(args[0]),
                "args": [str(value) for value in args[1:]],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(max(0.1, float(timeout)))
        sent = False
        try:
            try:
                connection.connect(str(self.rpc_socket_path()))
            except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError) as exc:
                raise _RPCUnavailable(str(exc)) from exc
            # Once sendall begins, a retry through a subprocess could duplicate
            # a mutation whose response was lost. Ambiguous failures therefore
            # fail closed instead of silently changing transports.
            sent = True
            connection.sendall(request)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = connection.recv(min(64 * 1024, RPC_MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > RPC_MAX_RESPONSE_BYTES:
                    raise InterfaceError("Interface daemon RPC response exceeded its safe limit")
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks).split(b"\n", 1)[0]
            if not raw:
                raise InterfaceError("Interface daemon RPC closed without a response")
            try:
                response = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InterfaceError("Interface daemon RPC returned invalid JSON") from exc
            if (
                not isinstance(response, dict)
                or response.get("version") != 1
                or response.get("id") != request_id
            ):
                raise InterfaceError("Interface daemon RPC returned a mismatched response")
            if response.get("ok") is True:
                return response.get("result")
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            code = str(error.get("code") or "RPC_ERROR")
            if code == "UNSUPPORTED_COMMAND":
                raise _RPCUnsupported(str(error.get("message") or code))
            if not check:
                return {}
            status = int(error.get("status") or 0)
            detail = str(error.get("message") or code)
            if status:
                detail = f"api {status}: {detail}"
            body = error.get("body")
            if body is not None:
                detail += "\n" + json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            raise InterfaceError(detail)
        except _RPCUnsupported:
            raise
        except _RPCUnavailable:
            raise
        except InterfaceError:
            raise
        except (OSError, TimeoutError) as exc:
            if not sent:
                raise _RPCUnavailable(str(exc)) from exc
            raise InterfaceError(f"Interface daemon RPC outcome is unknown: {exc}") from exc
        finally:
            connection.close()

    def run(
        self,
        args: list[str],
        *,
        json_mode: bool = True,
        input_text: str | None = None,
        timeout: int = 60,
        check: bool = True,
    ) -> Any:
        normalized_args = [str(arg) for arg in args if arg is not None]
        if json_mode and input_text is None and normalized_args:
            try:
                return self._run_rpc(
                    normalized_args,
                    timeout=timeout,
                    check=check,
                )
            except (_RPCUnavailable, _RPCUnsupported):
                # Older daemons and intentionally unsupported interactive
                # commands retain the proven subprocess compatibility path.
                pass
        cmd = self.base_cmd(json_mode=json_mode) + normalized_args
        proc = subprocess.run(
            cmd,
            input=input_text,
            cwd=str(self.cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise InterfaceError(detail or f"Interface command failed: {' '.join(cmd)}")
        if json_mode:
            return _parse_json_output(proc.stdout)
        return proc.stdout

    def popen(self, args: list[str], *, json_mode: bool = True) -> subprocess.Popen:
        cmd = self.base_cmd(json_mode=json_mode) + [str(arg) for arg in args if arg is not None]
        return subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
