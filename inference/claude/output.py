"""Reading a detached Claude worker's output file back."""
from __future__ import annotations

from core.progress import (
    provider_authentication_failed,
    provider_not_authenticated_message,
)
from inference.models import WorkerOutcome
from inference.parsing import json_events


def authentication_message(events, provider: str) -> str:
    """The login prompt to show, if any event reports an auth failure."""
    for event in events:
        values = []
        if event.get("error") or event.get("is_error"):
            values.extend([event.get("error"), event.get("errors")])
        if event.get("type") == "silicon.codex_app_error":
            values.append(event.get("message"))
        if event.get("method") == "error":
            params = event.get("params") or {}
            nested = params.get("error")
            values.extend([
                params.get("message"),
                nested.get("message") if isinstance(nested, dict) else nested,
            ])
        if provider_authentication_failed(*values):
            return provider_not_authenticated_message(provider)
    return ""


def read(raw: str) -> WorkerOutcome:
    """What a Claude worker's output says about how it went."""
    if not str(raw or "").strip():
        return WorkerOutcome(result="No output yet.")

    events = json_events(raw)
    if not events:
        return WorkerOutcome(result="No parseable output yet.")

    completed = [event for event in events if event.get("type") == "result"]
    outcome = WorkerOutcome(completed=bool(completed))

    if completed:
        terminal = completed[-1]
        status = str(terminal.get("subtype") or "").lower()
        failed = bool(terminal.get("is_error")) or any(
            marker in status
            for marker in ("error", "failed", "timeout", "cancel")
        )
        outcome.state = "failed" if failed else "completed"

    auth_message = authentication_message(events, "claude")
    if auth_message:
        outcome.auth_message = auth_message
        outcome.result = auth_message
        return outcome

    if completed and completed[-1].get("result"):
        outcome.result = completed[-1]["result"]
        return outcome

    texts = []
    seen = set()
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []) or []:
            if block.get("type") != "text":
                continue
            text = block.get("text", "").strip()
            if text and text not in seen:
                seen.add(text)
                texts.append(text)

    outcome.result = texts[-1] if texts else "Worker running, no text output yet."
    return outcome
