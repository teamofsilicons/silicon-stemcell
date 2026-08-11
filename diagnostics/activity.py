"""The activity trail, filed under the agent it belongs to.

Every tool call, every reply sent, every incoming message, and every attachment
(with its S3 link) goes to that contact's manager log —
``logs/manager/<contact_id>.log`` — so one file is one manager's whole history.
Anything without a contact goes to ``logs/silicon.log``.

The ``logs/`` folder is listed in ``.backupsilicon`` so the daily Interface
backup ships these off the box too. Logging is strictly best-effort: it must
never raise into a Silicon's hot path, so everything here swallows its own
errors.
"""
from __future__ import annotations

from helpers.paths import DATA_ROOT
from diagnostics.logs import LOGS_DIR, agent_log, silicon_log

PROJECT_ROOT = DATA_ROOT

__all__ = ["LOGS_DIR", "attachment", "incoming", "log", "reply", "tool_call", "url_from"]


def url_from(info) -> str:
    """Best-effort extraction of an S3/public URL from a media-info dict."""
    if not isinstance(info, dict):
        return ""
    for key in ("s3_url", "url", "download_url", "public_url", "media_url", "href", "location"):
        val = info.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def log(category: str, message: str = "", **fields) -> None:
    """Append one line to the log of whichever agent this belongs to.

    A ``contact=`` field names the manager; without one the line is the
    runtime's own and goes to ``logs/silicon.log``.
    """
    contact = str(fields.get("contact") or "")
    target = agent_log("manager", contact) if contact else silicon_log()
    target.event(category, message, **fields)


def tool_call(carbon_id: str, tool: str, args=None, result=None) -> None:
    if tool == "advertising_memory/update":
        # The complete model-supplied object is untrusted. Keep no fields:
        # unknown aliases could otherwise duplicate the advertised content.
        extra = {}
        result = "[Advertising memory result omitted]"
    elif tool == "work_update":
        # Work cards can contain files, browser sessions, blocker answers, and
        # full manager/Silicon transcripts. Glass already stores the visible
        # event; do not duplicate that content into local process logs.
        extra = {}
        if isinstance(args, dict):
            for key in (
                "action",
                "task_id",
                "todo_id",
                "blocker_id",
                "group_id",
                "invocation_id",
                "call_id",
            ):
                if key in args:
                    extra[key] = args[key]
        result = "[Work update result omitted]"
    elif isinstance(args, dict):
        extra = {k: v for k, v in args.items() if k != "tool"}
    else:
        extra = args
    log("TOOL", tool, contact=carbon_id, args=extra, result=result)


def reply(contact_id: str, message: str, result=None) -> None:
    log("REPLY", message, contact=contact_id, result=result)


def incoming(contact_id: str, event_type: str, body: str = "", media_id: str = "",
             attachment_url: str = "", event_id: str = "") -> None:
    log("INCOMING", body, contact=contact_id, type=event_type, event_id=event_id,
        media_id=media_id, s3=attachment_url)


def attachment(direction: str, contact_id: str = "", media_id: str = "", url: str = "",
               path: str = "", filename: str = "") -> None:
    log("ATTACHMENT", direction, contact=contact_id, media_id=media_id, s3=url,
        path=path, filename=filename)
