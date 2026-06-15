"""Daily activity logs.

Every tool call, every reply sent, every incoming message, and every attachment
(with its S3 link) is appended to ``logs/<UTC-date>.txt`` — plain text, one event
per line, human-readable. A new file rolls over each UTC day.

The ``logs/`` folder is listed in ``.backupsilicon`` so the daily Glass backup
ships these off the box too. Logging is strictly best-effort: it must never
raise into the silicon's hot path, so everything here swallows its own errors.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = PROJECT_ROOT / "logs"
_MAX_FIELD = 2000


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _today_path() -> Path:
    return LOGS_DIR / f"{_now():%Y-%m-%d}.txt"


def _fmt(value) -> str:
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    text = str(value).replace("\r", " ").replace("\n", "\\n").strip()
    if len(text) > _MAX_FIELD:
        text = text[:_MAX_FIELD] + f"…(+{len(text) - _MAX_FIELD} chars)"
    return text


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
    """Append one timestamped line: ``[ts] CATEGORY | message | k=v | k=v``."""
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        line = f"[{_now():%Y-%m-%dT%H:%M:%S}Z] {category}"
        if message != "" and message is not None:
            line += f" | {_fmt(message)}"
        for key, val in fields.items():
            if val in (None, "", [], {}):
                continue
            line += f" | {key}={_fmt(val)}"
        with open(_today_path(), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def tool_call(carbon_id: str, tool: str, args=None, result=None) -> None:
    extra = args
    if isinstance(args, dict):
        extra = {k: v for k, v in args.items() if k != "tool"}
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
