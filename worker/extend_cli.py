"""Worker-safe command-line bridge for Silicon Extend.

The worker action is the only command-line argument. Tool keys, arguments, and
notes arrive as one JSON object on stdin, while the originating contact is
inherited from ``worker.handler``. This keeps operation data out of the Python
CLI's argument contract; the surrounding worker shell and transcript are not a
confidential transport.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from core import extend

CONTACT_ENV = "SILICON_EXTEND_ACTING_CARBON_ID"
LEGACY_CONTACT_ENV = "SILICON_EXTEND_CONTACT_ID"
ROOM_ENV = "SILICON_EXTEND_ROOM_ID"
MAX_STDIN_BYTES = 128_000
MAX_OUTPUT_BYTES = 48_000
MAX_RESULT_STRING = 8_000
MAX_COLLECTION_ITEMS = 100
MAX_VALUE_DEPTH = 10

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INPUT = 2
EXIT_EXTEND = 3

_ACTIONS = {"list", "execute", "request-setup"}
_SECRET_KEYS = {
    "accesskey",
    "accesstoken",
    "apikey",
    "auth",
    "authorization",
    "clientsecret",
    "connecturl",
    "cookie",
    "credentials",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "provideraccountid",
    "refreshtoken",
    "secret",
    "secretkey",
    "setcookie",
    "setupurl",
    "token",
}
_SCHEMA_LITERAL_KEYS = {"default", "example", "examples"}
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:ak|ck)_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:gh[opusr]|xox[baprs])_[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{12,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"
        r"(?:\.[A-Za-z0-9_-]{5,})?\b"
    ),
    re.compile(
        r"(?i)\b(?:access[_-]?token|api[_-]?key|authorization|"
        r"client[_-]?secret|password|refresh[_-]?token|secret)"
        r"\s*[:=]\s*[^\s,;&]+"
    ),
    re.compile(
        r"(?i)([?&](?:access[_-]?token|api[_-]?key|authorization|"
        r"client[_-]?secret|password|refresh[_-]?token|secret)=)"
        r"[^&#\s]+"
    ),
)


def _normal_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _sensitive_key(value: Any) -> bool:
    key = _normal_key(value)
    if key in _SECRET_KEYS:
        return True
    return any(
        key.endswith(suffix)
        for suffix in (
            "accesstoken",
            "apikey",
            "clientsecret",
            "privatekey",
            "refreshtoken",
            "secretkey",
        )
    )


def _mark_truncated(marker: list[bool] | None) -> None:
    if marker is not None:
        marker[0] = True


def _scrub_text(
    value: Any,
    *,
    truncated: list[bool] | None = None,
) -> str:
    text = str(value or "")
    for pattern in _SECRET_TEXT_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    if len(text) > MAX_RESULT_STRING:
        _mark_truncated(truncated)
        return text[:MAX_RESULT_STRING] + "…"
    return text


def _sanitize_value(
    value: Any,
    *,
    depth: int = 0,
    truncated: list[bool] | None = None,
) -> Any:
    if depth >= MAX_VALUE_DEPTH:
        _mark_truncated(truncated)
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                _mark_truncated(truncated)
                clean["_truncated"] = True
                break
            key = _scrub_text(raw_key, truncated=truncated)
            if len(key) > 200:
                _mark_truncated(truncated)
                key = key[:200]
            if _sensitive_key(key):
                continue
            clean[key] = _sanitize_value(
                item,
                depth=depth + 1,
                truncated=truncated,
            )
        return clean
    if isinstance(value, (list, tuple)):
        clean_items = [
            _sanitize_value(
                item,
                depth=depth + 1,
                truncated=truncated,
            )
            for item in value[:MAX_COLLECTION_ITEMS]
        ]
        if len(value) > MAX_COLLECTION_ITEMS:
            _mark_truncated(truncated)
            clean_items.append("[TRUNCATED]")
        return clean_items
    if isinstance(value, str):
        return _scrub_text(value, truncated=truncated)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _scrub_text(value, truncated=truncated)


def _sanitize_schema(
    value: Any,
    *,
    depth: int = 0,
    truncated: list[bool] | None = None,
) -> Any:
    """Keep invocation structure while dropping value-bearing annotations."""

    if depth >= MAX_VALUE_DEPTH:
        _mark_truncated(truncated)
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                _mark_truncated(truncated)
                clean["_truncated"] = True
                break
            key = _scrub_text(raw_key, truncated=truncated)
            if len(key) > 200:
                _mark_truncated(truncated)
                key = key[:200]
            if _normal_key(key) in _SCHEMA_LITERAL_KEYS:
                continue
            clean[key] = _sanitize_schema(
                item,
                depth=depth + 1,
                truncated=truncated,
            )
        return clean
    if isinstance(value, (list, tuple)):
        clean_items = [
            _sanitize_schema(
                item,
                depth=depth + 1,
                truncated=truncated,
            )
            for item in value[:MAX_COLLECTION_ITEMS]
        ]
        if len(value) > MAX_COLLECTION_ITEMS:
            _mark_truncated(truncated)
            clean_items.append("[TRUNCATED]")
        return clean_items
    return _sanitize_value(
        value,
        depth=depth,
        truncated=truncated,
    )


def _directory_rows(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    direct = payload.get("tools")
    candidates: list[Any]
    if isinstance(direct, list):
        candidates = direct
    else:
        candidates = []
        integrations = payload.get("integrations")
        if isinstance(integrations, list):
            for integration in integrations:
                if isinstance(integration, Mapping):
                    tools = integration.get("tools")
                    if isinstance(tools, list):
                        candidates.extend(tools)

    rows: list[dict[str, Any]] = []
    truncated = [False]
    for item in candidates:
        if not isinstance(item, Mapping) or item.get("enabled") is False:
            continue
        key = str(item.get("key") or item.get("tool_key") or "").strip()
        if not key:
            continue
        if len(rows) >= MAX_COLLECTION_ITEMS:
            truncated[0] = True
            break
        safe_key = _scrub_text(key, truncated=truncated)
        if len(safe_key) > 300:
            truncated[0] = True
            safe_key = safe_key[:300]
        safe_name = _scrub_text(
            item.get("name") or item.get("display_name") or key,
            truncated=truncated,
        )
        if len(safe_name) > 500:
            truncated[0] = True
            safe_name = safe_name[:500]
        setup_status = _scrub_text(
            item.get("setup_status")
            or item.get("connection_status")
            or "unknown",
            truncated=truncated,
        )
        if len(setup_status) > 100:
            truncated[0] = True
            setup_status = setup_status[:100]
        row = {
            "key": safe_key,
            "name": safe_name,
            "description": _scrub_text(
                item.get("description") or "",
                truncated=truncated,
            ),
            "setup_status": setup_status,
            "input_schema": _sanitize_schema(
                item.get("input_schema") or item.get("input_parameters") or {},
                truncated=truncated,
            ),
        }
        rows.append(row)
    return rows, truncated[0]


def _utf8_prefix(value: str, limit: int) -> str:
    return value.encode("utf-8")[: max(0, limit)].decode("utf-8", errors="ignore")


def _encoded(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _bounded_payload(payload: dict[str, Any]) -> str:
    payload.setdefault("truncated", False)
    rendered = _encoded(payload)
    if len(rendered.encode("utf-8")) <= MAX_OUTPUT_BYTES:
        return rendered

    compact: dict[str, Any] = {
        "ok": bool(payload.get("ok")),
        "action": str(payload.get("action") or ""),
        "truncated": True,
    }
    if isinstance(payload.get("tools"), list):
        compact["tools"] = []
        for row in payload["tools"]:
            candidate = {**compact, "tools": [*compact["tools"], row]}
            if len(_encoded(candidate).encode("utf-8")) > MAX_OUTPUT_BYTES - 256:
                break
            compact["tools"].append(row)
        return _encoded(compact)

    if payload.get("tool"):
        compact["tool"] = _scrub_text(payload["tool"])[:300]
    source = payload.get("result", payload.get("error", ""))
    preview = _encoded(_sanitize_value(source))
    compact["result_preview"] = _utf8_prefix(
        preview,
        MAX_OUTPUT_BYTES - len(_encoded(compact).encode("utf-8")) - 512,
    )
    rendered = _encoded(compact)
    if len(rendered.encode("utf-8")) <= MAX_OUTPUT_BYTES:
        return rendered
    compact["result_preview"] = _utf8_prefix(
        compact["result_preview"],
        MAX_OUTPUT_BYTES // 2,
    )
    return _encoded(compact)


def _write(stdout, payload: dict[str, Any]) -> None:
    stdout.write(_bounded_payload(payload) + "\n")
    stdout.flush()


def _error(
    stdout,
    *,
    action: str,
    code: str,
    message: str,
    exit_code: int,
) -> int:
    _write(
        stdout,
        {
            "ok": False,
            "action": action,
            "error": {
                "code": _scrub_text(code)[:100],
                "message": _scrub_text(message),
            },
        },
    )
    return exit_code


def _read_json_object(stdin) -> tuple[dict[str, Any] | None, str]:
    try:
        raw = stdin.read(MAX_STDIN_BYTES + 1)
    except (OSError, ValueError):
        return None, "Could not read stdin."
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    else:
        raw_bytes = bytes(raw or b"")
    if len(raw_bytes) > MAX_STDIN_BYTES:
        return None, "stdin JSON is too large."
    try:
        value = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None, "stdin must contain one valid JSON object."
    if not isinstance(value, dict):
        return None, "stdin JSON must be an object."
    return value, ""


def _validate_body(
    body: Mapping[str, Any],
    *,
    action: str,
) -> tuple[str, dict[str, Any] | str | None, str]:
    allowed = (
        {"tool", "arguments"}
        if action == "execute"
        else {"tool", "note"}
    )
    if any(key not in allowed for key in body):
        return "", None, "stdin JSON contains unsupported fields."
    tool = body.get("tool")
    if (
        not isinstance(tool, str)
        or not tool.strip()
        or len(tool.strip()) > 300
        or any(ord(char) < 32 for char in tool)
    ):
        return "", None, "tool must be a non-empty string of at most 300 characters."
    if action == "execute":
        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            return "", None, "arguments must be a JSON object."
        return tool.strip(), arguments, ""
    note = body.get("note", "")
    if not isinstance(note, str) or len(note) > 500:
        return "", None, "note must be a string of at most 500 characters."
    return tool.strip(), note, ""


def _contact_context(environ: Mapping[str, str]) -> str:
    value = str(
        environ.get(CONTACT_ENV)
        or environ.get(LEGACY_CONTACT_ENV)
        or ""
    ).strip()
    if (
        not value
        or len(value) > 300
        or any(ord(char) < 32 for char in value)
    ):
        return ""
    return value


def _safe_error_code(value: Any) -> str:
    code = _scrub_text(value).lower()
    if (
        "[redacted]" in code
        or not re.fullmatch(r"[a-z][a-z0-9_-]{1,99}", code)
    ):
        return "extend_error"
    return code


def _operation_response(
    action: str,
    tool: str,
    operation_result: Mapping[str, Any],
) -> dict[str, Any]:
    truncated = [False]
    response = {
        "ok": True,
        "action": action,
        "tool": tool,
        "truncated": False,
    }
    if operation_result.get("setup_requested"):
        response["setup_requested"] = True
        request_id = str(operation_result.get("request_id") or "")
        if request_id:
            response["request_id"] = _scrub_text(
                request_id,
                truncated=truncated,
            )
        response["result"] = "A durable setup request was sent as a chat message."
    else:
        response["result"] = _sanitize_value(
            operation_result.get("result"),
            truncated=truncated,
        )
    response["truncated"] = truncated[0]
    return response


def run(
    argv: Sequence[str] | None = None,
    *,
    stdin=None,
    stdout=None,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    input_stream = sys.stdin.buffer if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    environment = os.environ if environ is None else environ

    if len(args) != 1 or args[0] not in _ACTIONS:
        return _error(
            output_stream,
            action="",
            code="usage",
            message=(
                "Usage: python -m worker.extend_cli "
                "{list|execute|request-setup}; operation data belongs on stdin."
            ),
            exit_code=EXIT_INPUT,
        )
    action = args[0]
    if action == "list":
        try:
            directory = extend.load_directory(force=True, strict=True)
        except extend.ExtendError as exc:
            return _error(
                output_stream,
                action=action,
                code=_safe_error_code(exc.code),
                message="Silicon Extend is unavailable.",
                exit_code=EXIT_EXTEND,
            )
        except Exception:  # noqa: BLE001 - keep the worker bridge fail-closed.
            return _error(
                output_stream,
                action=action,
                code="internal_error",
                message="Silicon Extend failed without returning a directory.",
                exit_code=EXIT_INTERNAL,
            )
        rows, was_truncated = _directory_rows(
            directory if isinstance(directory, Mapping) else {}
        )
        _write(
            output_stream,
            {
                "ok": True,
                "action": action,
                "tools": rows,
                "truncated": was_truncated,
            },
        )
        return EXIT_OK

    contact_id = _contact_context(environment)
    if not contact_id:
        return _error(
            output_stream,
            action=action,
            code="context_missing",
            message="This command is available only inside a Silicon worker.",
            exit_code=EXIT_INPUT,
        )
    body, read_error = _read_json_object(input_stream)
    if body is None:
        return _error(
            output_stream,
            action=action,
            code="invalid_input",
            message=read_error,
            exit_code=EXIT_INPUT,
        )
    tool, operation_data, validation_error = _validate_body(body, action=action)
    if validation_error:
        return _error(
            output_stream,
            action=action,
            code="invalid_input",
            message=validation_error,
            exit_code=EXIT_INPUT,
        )

    try:
        if action == "execute":
            operation_result = extend.execute_tool_result(
                tool,
                operation_data,
                carbon_id=contact_id,
            )
        else:
            operation_result = extend.request_setup_result(
                tool,
                note=operation_data,
                carbon_id=contact_id,
            )
    except extend.ExtendError as exc:
        return _error(
            output_stream,
            action=action,
            code=_safe_error_code(exc.code),
            message="Silicon Extend could not complete the request.",
            exit_code=EXIT_EXTEND,
        )
    except Exception:  # noqa: BLE001 - keep the worker bridge fail-closed.
        return _error(
            output_stream,
            action=action,
            code="internal_error",
            message="Silicon Extend failed without returning a result.",
            exit_code=EXIT_INTERNAL,
        )
    _write(
        output_stream,
        _operation_response(action, tool, operation_result),
    )
    return EXIT_OK


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
