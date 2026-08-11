"""Every command Silicon sends to Interface.

One method per CLI verb, grouped by what it is for: who we are and which rooms
exist, sending and reading, work cards, and the control surface (remote
browser, take-back, crons). The transport underneath is
:class:`~interface.rpc.Transport`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from interface import constants
from interface.errors import InterfaceError, WorkCallMutationError
from interface.rpc import Transport


class InterfaceClient(Transport):
    """The Interface CLI, as methods."""


    def whoami(self) -> Any:
        return self.run(["me"], timeout=30)

    def rooms_list(self) -> Any:
        return self.run(["rooms", "list"], timeout=30)

    def room_members(self, room_id: str) -> Any:
        payload = self.run(["rooms", "show", room_id, "--limit", "0"], timeout=45)
        if isinstance(payload, dict) and "members" in payload:
            return payload.get("members") or []
        return payload

    def ensure_direct_room(self, contact_type: str, fixed_id: str) -> Any:
        return self.run(["rooms", "direct", contact_type, fixed_id], timeout=60)

    def daemon_status(self) -> Any:
        """Return the CLI v2 durable-listener status and inbox location."""
        payload = self.run(["daemon", "status"], timeout=30, check=False)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("running"), bool)
            or not str(payload.get("inbox") or "").strip()
            or not isinstance(payload.get("cursors"), dict)
        ):
            raise InterfaceError(
                "Silicon Interface CLI v2 is required: `si --json daemon status` "
                "did not return the durable listener contract."
            )
        return payload

    def daemon_local_status(self) -> dict[str, Any]:
        """Check the CLI-owned daemon without cold-starting the Node CLI."""
        root = Path(
            os.environ.get("SILICON_INTERFACE_ROOT") or self.cwd
        ).expanduser().resolve()
        state_dir = root / ".silicon-interface"
        pid_file = state_dir / "daemon.pid"
        pid: int | None = None
        try:
            parsed = int(pid_file.read_text(encoding="utf-8").strip())
            if parsed > 0:
                pid = parsed
        except (OSError, TypeError, ValueError):
            pid = None
        running = False
        if pid is not None:
            try:
                os.kill(pid, 0)
                running = True
            except (OSError, ProcessLookupError, PermissionError):
                running = False
        inbox_value = str(
            os.environ.get("SILICON_INTERFACE_INBOX") or ""
        ).strip()
        inbox = (
            Path(inbox_value).expanduser()
            if inbox_value
            else state_dir / "inbox.jsonl"
        )
        return {
            "running": running,
            "pid": pid,
            "inbox": str(inbox),
        }

    def daemon_start(self) -> str:
        """Start the single CLI-owned listener; this command prints prose."""
        return str(
            self.run(
                ["daemon", "start"],
                json_mode=False,
                timeout=60,
            )
            or ""
        )

    def inbox_path(self) -> Path:
        status = self.daemon_status()
        value = str(status.get("inbox") or "").strip()
        return Path(value).expanduser() if value else constants.DEFAULT_INBOX_FILE

    def send(
        self,
        room_id: str,
        message: str,
        progress_group_id: str = "",
        work_continues: bool = False,
        client_id: str = "",
    ) -> Any:
        args = ["send", room_id, message]
        if client_id:
            args.extend(["--client-id", str(client_id)])
        if progress_group_id:
            args.extend(["--group", progress_group_id])
        if work_continues:
            args.append("--work-continues")
        return self.run(args, timeout=60)

    def send_file(self, room_id: str, path: str) -> Any:
        return self.run(["send-file", room_id, path], timeout=120)

    def tts(self, room_id: str, text: str) -> Any:
        return self.run(["tts", "--room", room_id, text], timeout=180)

    def read(self, room_id: str, event_id: str) -> Any:
        return self.run(["read", room_id, event_id], timeout=30, check=False)

    def media_show(self, media_id: str) -> Any:
        return self.run(["media", "show", media_id], timeout=30)

    def stt(self, value: str) -> Any:
        return self.run(["stt", value], timeout=180)

    def progress(
        self,
        room_id: str,
        group: str,
        state: str,
        message: str,
        frame_id: str,
        task_id: str = "",
        revision: int | None = None,
        occurred_at: str = "",
        progress_pct: float | None = None,
        summary: str = "",
    ) -> Any:
        args = ["progress", room_id, state, "--group", group]
        if message:
            args.extend(["--note", message])
        args.extend(["--frame", frame_id])
        if task_id:
            args.extend(["--task", task_id])
        if revision is not None:
            args.extend(["--revision", str(revision)])
        if occurred_at:
            args.extend(["--at", occurred_at])
        if progress_pct is not None:
            args.extend(["--pct", str(progress_pct)])
        if summary:
            args.extend(["--summary", summary])
        return self.run(args, timeout=30)

    @staticmethod
    def _compact_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _work_mutation(self, args: list[str], payload: dict[str, Any]) -> Any:
        return self.run(
            [*args, "--data", self._compact_json(payload)],
            timeout=60,
        )

    def _work_call_patch_mutation(
        self,
        args: list[str],
        payload: dict[str, Any],
    ) -> Any:
        try:
            return self._work_mutation(args, payload)
        except InterfaceError as exc:
            # CLI 2.0.2 prints Glass's structured failure after an "api NNN"
            # prefix. Keep only retry metadata; never retain the command or
            # transcript-bearing payload in retry state.
            detail = str(exc)
            status_match = re.search(r"\bapi\s+([1-5][0-9]{2})\b", detail)
            revision_match = re.search(
                r'"current"\s*:\s*\{[^{}]*"revision"\s*:\s*(\d+)',
                detail,
            )
            code_match = re.search(r'"code"\s*:\s*"([^"]+)"', detail)
            status = int(status_match.group(1)) if status_match else 0
            revision = (
                int(revision_match.group(1)) if revision_match else None
            )
            raise WorkCallMutationError(
                status_code=status,
                code=code_match.group(1) if code_match else "",
                current_revision=revision,
                retryable=status
                in {0, 408, 409, 425, 429, 500, 502, 503, 504},
            ) from exc

    def work_task_create(self, payload: dict[str, Any]) -> Any:
        return self._work_mutation(["work", "task", "create"], payload)

    def work_task_show(self, task_id: str) -> Any:
        return self.run(["work", "task", "show", task_id], timeout=60)

    def work_task_patch(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(["work", "task", "patch", task_id], payload)

    def work_todo_add(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(["work", "todo", "add", task_id], payload)

    def work_todo_patch(
        self,
        task_id: str,
        todo_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "todo", "patch", task_id, todo_id],
            payload,
        )

    def work_milestone_create(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(
            ["work", "milestone", "update", task_id],
            payload,
        )

    def work_blocker_create(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(
            ["work", "blocker", "create", task_id],
            payload,
        )

    def work_blocker_resolve(
        self,
        task_id: str,
        blocker_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "blocker", "resolve", task_id, blocker_id],
            payload,
        )

    def work_worker_group_create(
        self,
        task_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "worker-group", "create", task_id],
            payload,
        )

    def work_worker_group_patch(
        self,
        task_id: str,
        group_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "worker-group", "patch", task_id, group_id],
            payload,
        )

    def work_worker_create(
        self,
        task_id: str,
        group_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            ["work", "worker", "create", task_id, group_id],
            payload,
        )

    def work_worker_patch(
        self,
        task_id: str,
        group_id: str,
        invocation_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(
            [
                "work",
                "worker",
                "patch",
                task_id,
                group_id,
                invocation_id,
            ],
            payload,
        )

    def work_call_create(self, task_id: str, payload: dict[str, Any]) -> Any:
        return self._work_mutation(
            ["work", "call", "create", task_id],
            payload,
        )

    def work_standalone_call_create(self, payload: dict[str, Any]) -> Any:
        return self._work_mutation(
            ["work", "call", "create"],
            payload,
        )

    def work_call_patch(
        self,
        task_id: str,
        call_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_call_patch_mutation(
            ["work", "call", "patch", task_id, call_id],
            payload,
        )

    def work_standalone_call_patch(
        self,
        call_id: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_call_patch_mutation(
            ["work", "call", "patch", call_id],
            payload,
        )

    def work_task_transition(
        self,
        task_id: str,
        transition: str,
        payload: dict[str, Any],
    ) -> Any:
        return self._work_mutation(["work", transition, task_id], payload)

    def remote_browser(self, room_id: str, url: str, ttl_minutes: int) -> Any:
        return self.run(["remote-browser", room_id, url, "--ttl-minutes", str(ttl_minutes)], timeout=30)

    def take_back_complete(self, request_id: str, replacement: str) -> Any:
        return self.run(["take-back", "complete", request_id, replacement], timeout=60)

    def take_back_event(self, event_id: str, reason: str = "", force: bool = False) -> Any:
        args = ["take-back", event_id]
        if reason:
            args.extend(["--reason", reason])
        if force:
            args.append("--force")
        return self.run(args, timeout=60)

    def crons_list(self) -> Any:
        return self.run(["crons", "list", "--mine"], timeout=45)

    def cron_create(self, trigger: str, task: str, targets: list[dict[str, Any]]) -> Any:
        # The Interface CLI takes recipients as repeated `--target kind:id` flags
        # (kind ∈ carbon|silicon), NOT a single `--targets` JSON blob — passing
        # JSON makes it fail with "Pass at least one --target kind:id."
        args = ["crons", "create", "--trigger", trigger, "--task", task]
        for t in targets:
            kind = str(t.get("kind") or "").strip().lower()
            ident = str(
                t.get("id") or t.get("carbon_id") or t.get("silicon_id") or ""
            ).strip()
            if not kind:
                kind = "carbon" if t.get("carbon_id") else "silicon" if t.get("silicon_id") else ""
            if kind and ident:
                args.extend(["--target", f"{kind}:{ident}"])
        return self.run(args, timeout=60)

    def cron_update(self, cron_id: str, **updates: Any) -> Any:
        args = ["crons", "update", cron_id]
        for key in ("trigger", "task", "active"):
            if key in updates and updates[key] is not None:
                args.extend([f"--{key}", str(updates[key]).lower() if isinstance(updates[key], bool) else str(updates[key])])
        return self.run(args, timeout=60)

    def cron_delete(self, cron_id: str) -> Any:
        return self.run(["crons", "delete", cron_id], timeout=60)


