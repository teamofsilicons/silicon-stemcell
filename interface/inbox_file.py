"""The durable inbox file, and where this Silicon has read up to.

The Interface CLI appends complete frames to a JSONL file. Stemcell commits its
byte offset only after a whole frame has been interpreted, which is what makes
a crash mid-turn safe: the frame is read again rather than lost.

A rotated or truncated file is detected by (device, inode, ctime) and restarts
the read from zero, because an offset into a different file means nothing.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from helpers.state import file_lock, read_json, write_json
from helpers.timefmt import utc_iso as _utc_iso
from interface import constants
from interface.constants import INBOX_READ_CHUNK_BYTES
from interface.models import InboxRecord

_inbox_scan_lock = threading.Lock()
_inbox_scan_state: dict[str, Any] = {}


def _inbox_file_id(stat_result: os.stat_result) -> str:
    return f"{getattr(stat_result, 'st_dev', 0)}:{getattr(stat_result, 'st_ino', 0)}"


def _load_inbox_consumer() -> dict[str, Any]:
    state = read_json(constants.INBOX_CONSUMER_FILE, {})
    if not isinstance(state, dict) or state.get("version") != 1:
        return {"version": 1, "path": "", "file_id": "", "offset": 0}
    try:
        offset = max(0, int(state.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    return {
        "version": 1,
        "path": str(state.get("path") or ""),
        "file_id": str(state.get("file_id") or ""),
        "offset": offset,
    }


def _save_inbox_consumer(path: str, file_id: str, offset: int) -> None:
    write_json(
        constants.INBOX_CONSUMER_FILE,
        {
            "version": 1,
            "path": path,
            "file_id": file_id,
            "offset": max(0, int(offset)),
            "updated_at": _utc_iso(),
        },
    )


def _read_new_inbox_records(path: Path, *, max_records: int = 500) -> list[InboxRecord]:
    """Read complete, not-yet-committed CLI inbox lines without acknowledging them.

    The in-memory scan offset prevents duplicate queueing while this process is
    alive. The durable offset advances only after the main loop has interpreted
    a record, so a crash before interpretation replays it on restart.
    """
    resolved = str(path.expanduser().resolve())
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return []
    file_id = _inbox_file_id(stat_result)

    with _inbox_scan_lock:
        cursor = _load_inbox_consumer()
        scan = dict(_inbox_scan_state)
        if (
            scan.get("path") != resolved
            or scan.get("file_id") != file_id
            or int(scan.get("offset") or 0) > stat_result.st_size
        ):
            if (
                cursor.get("path") == resolved
                and cursor.get("file_id") == file_id
                and int(cursor.get("offset") or 0) <= stat_result.st_size
            ):
                offset = int(cursor.get("offset") or 0)
            else:
                # Rotation, truncation, or first use: scan the replacement from
                # its beginning. Processed event IDs make snapshot replay safe.
                offset = 0
            scan = {"path": resolved, "file_id": file_id, "offset": offset}

        records: list[InboxRecord] = []
        bytes_read = 0
        with path.open("rb") as inbox:
            inbox.seek(int(scan["offset"]))
            while len(records) < max_records and bytes_read < INBOX_READ_CHUNK_BYTES:
                start = inbox.tell()
                line = inbox.readline()
                if not line:
                    break
                # The daemon may be between its append and newline. Leave the
                # partial line unscanned until the next pass.
                if not line.endswith(b"\n"):
                    inbox.seek(start)
                    break
                end = inbox.tell()
                bytes_read += end - start
                try:
                    payload = json.loads(line.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("inbox frame is not an object")
                    frame = payload
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    # A complete malformed line cannot become valid later.
                    # Advance past it without echoing potentially private data.
                    frame = {"type": "_invalid_inbox_line"}
                records.append(
                    InboxRecord(
                        frame=frame,
                        path=resolved,
                        file_id=file_id,
                        end_offset=end,
                    )
                )
            scan["offset"] = inbox.tell()
        _inbox_scan_state.clear()
        _inbox_scan_state.update(scan)
        return records


def _commit_inbox_record(record: InboxRecord) -> None:
    if not record.path or not record.file_id or record.end_offset <= 0:
        return
    with file_lock(constants.INBOX_CONSUMER_FILE):
        cursor = _load_inbox_consumer()
        if (
            cursor.get("path") == record.path
            and cursor.get("file_id") == record.file_id
            and int(cursor.get("offset") or 0) >= record.end_offset
        ):
            return
        _save_inbox_consumer(record.path, record.file_id, record.end_offset)
