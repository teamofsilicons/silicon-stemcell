"""What must never leave this process, and how it is removed.

Provider output carries private manager tool calls, advertising memory, raw
commands and their output, and occasionally a credential. Every one of those
has a path to a Carbon or to a durable log, so each is replaced by a marker
here — before it is rendered, not after.

The functions fail closed: when a payload cannot be parsed cleanly, it is
redacted rather than passed through.
"""
from __future__ import annotations

import json
import os
import posixpath
import re


def stringify_command(command):
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


_PRIVATE_MANAGER_MARKER = "[private manager tool invocation omitted]"
_ADVERTISING_CONTENT_MARKER = "[advertising memory content omitted]"
_COMMAND_MARKER = "[command omitted]"
_COMMAND_OUTPUT_MARKER = "[command output omitted]"
_ADVERTISING_PATH_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9._-])prompts[/\\]+advertising(?:[/\\]|$)"
)
# Both spellings of the state directory: an upgraded instance still has the
# legacy copy on disk, and a draft leaking through either path is the same leak.
_DRAFT_ARCHIVE_PATH_RE = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9._-])"
    r"(?:core[/\\]+interface_state|interface[/\\]+state)[/\\]+"
    r"team_context_drafts(?:[/\\]|$)"
)
_PATH_TOKEN_RE = re.compile(
    r"(?:[A-Za-z]:)?[/\\]?[A-Za-z0-9._*?\[\]-]+"
    r"(?:[/\\]+[A-Za-z0-9._*?\[\]-]+)+"
)
_SENSITIVE_ERROR_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(?:sk|sct|scs|ghp|github_pat)_[a-z0-9_-]{8,}\b"), "[redacted credential]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted credential]"),
    (
        re.compile(
            r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|secret|password)"
            r"\s*[:=]\s*['\"]?[^\s,'\"]+"
        ),
        r"\1=[redacted]",
    ),
)


def compact(text, limit=240):
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        return text[:limit - 1] + "..."
    return text


def contains_private_manager_tool(value):
    text = str(value or "")
    # JSON's solidus escape is semantically identical to "/". Normalize it
    # before the malformed/plain-text fallback so a truncated invocation still
    # fails closed instead of printing its content.
    normalized_text = re.sub(r"\\u002[fF]", "/", text).replace("\\/", "/")
    decoder = json.JSONDecoder()
    index = 0
    while index < len(normalized_text):
        start = normalized_text.find("{", index)
        if start < 0:
            break
        try:
            parsed, consumed = decoder.raw_decode(normalized_text[start:])
        except (json.JSONDecodeError, ValueError):
            index = start + 1
            continue
        index = start + max(consumed, 1)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("tools"), list):
            continue
        for tool_spec in parsed["tools"]:
            if not isinstance(tool_spec, dict):
                continue
            tool = str(tool_spec.get("tool") or "")
            if (
                tool == "advertising_memory/update"
                or tool == "work_update"
                or tool in {"trust/list", "trust/get", "trust/set"}
            ):
                return True
    # Fail closed for incomplete/plain private invocations that still use the
    # canonical spelling. Complete escaped JSON is handled by raw_decode above.
    return (
        "advertising_memory/update" in normalized_text
        or bool(
            re.search(
                r"""["']tool["']\s*:\s*["']work_update["']""",
                normalized_text,
            )
        )
        or bool(
            re.search(
                r"""["']tool["']\s*:\s*["']trust/(?:list|get|set)["']""",
                normalized_text,
            )
        )
    )


def contains_advertising_memory_reference(value):
    text = re.sub(r"\\u002[fF]", "/", str(value or "")).replace("\\/", "/")
    # Inspect a quote-stripped copy as well: shell commands can legally quote
    # individual path components. Lexically collapse "." and ".." segments so
    # aliases such as prompts/x/../advertising cannot evade content redaction.
    candidates = [text, text.replace('"', "").replace("'", "")]
    normalized_candidates: list[str] = []
    for candidate in candidates:
        slash_normalized = candidate.replace("\\", "/")
        normalized_candidates.append(slash_normalized)
        normalized_candidates.extend(
            posixpath.normpath(match.group(0))
            for match in _PATH_TOKEN_RE.finditer(slash_normalized)
        )
    text = "\n".join(normalized_candidates)
    return bool(
        _ADVERTISING_PATH_RE.search(text)
        or _DRAFT_ARCHIVE_PATH_RE.search(text)
    )


def redact_private_manager_output(value, limit=240):
    """Keep private manager-tool payloads out of progress and terminal logs."""
    text = str(value or "")
    if contains_private_manager_tool(text):
        return _PRIVATE_MANAGER_MARKER
    if contains_advertising_memory_reference(text):
        return _ADVERTISING_CONTENT_MARKER
    return compact(text, limit=limit)



def redact_diagnostic_text(value, limit=500):
    """Bound diagnostic text and redact common credential formats."""
    text = " ".join(str(value or "").split())
    private_safe = redact_private_manager_output(text, limit=limit)
    if private_safe in {_PRIVATE_MANAGER_MARKER, _ADVERTISING_CONTENT_MARKER}:
        return private_safe
    for pattern, replacement in _SENSITIVE_ERROR_PATTERNS:
        text = pattern.sub(replacement, text)
    return compact(text, limit=limit)


_PROVIDER_AUTH_FAILURE_MARKERS = (
    "authentication_failed",
    "authentication failed",
    "failed to authenticate",
    "not authenticated",
    "not logged in",
    "login required",
    "please run /login",
    "oauth session expired",
    "oauth token expired",
    "invalid api key",
    "incorrect api key",
    "401 unauthorized",
)




def _log_output_limit():
    try:
        return max(0, int(os.environ.get("SILICON_LOG_OUTPUT_CHARS", "4000")))
    except (TypeError, ValueError):
        return 4000


def redact_secrets(text):
    """Strip credential shapes, keeping line structure intact.

    ``redact_diagnostic_text`` collapses whitespace and blanks anything that
    looks like private manager output — right for a Carbon-visible surface,
    wrong for a process log you are reading to debug a command. This does the
    credential scrubbing only.
    """
    value = str(text or "")
    for pattern, replacement in _SENSITIVE_ERROR_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _output_text(value):
    """Flatten a tool result into text. Claude sends content blocks; Codex sends a string."""
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return str(value or "")


def _output_block(value, limit):
    """Render tool output as indented lines, or [] when there is none."""
    text = _log_safe(_output_text(value)).rstrip()
    if not text:
        return []
    if limit and len(text) > limit:
        dropped = len(text) - limit
        text = text[:limit] + f"\n… (+{dropped} more characters)"
    return [f"    │ {line}" for line in text.splitlines()]


# Correlates a tool call to its result across two stream events, so the result
# can be printed under the command that produced it.
_LOG_CALLS_KEY = "_log_tool_calls"


def _log_safe(text):
    """Credential-scrub for the process log, keeping the two hard boundaries.

    A terminal log is for the operator, so commands and output belong in it.
    Peer advertising memory and private manager tool payloads do not — those
    stay redacted everywhere, including here.
    """
    value = str(text or "")
    if contains_private_manager_tool(value):
        return _PRIVATE_MANAGER_MARKER
    if contains_advertising_memory_reference(value):
        return _ADVERTISING_CONTENT_MARKER
    return redact_secrets(value)


def _reads_private_content(*values):
    """True when a tool call is reaching for advertising memory.

    The *result* of `cat prompts/advertising/peer.md` is the file's contents,
    which contain no path to match on — so the call has to be recognised when it
    starts and its output suppressed by id, not by inspecting the output.
    """
    return any(
        contains_advertising_memory_reference(str(value or ""))
        or contains_private_manager_tool(str(value or ""))
        for value in values
    )


def _tool_call_summary(tool_name, tool_input):
    """One line describing what a tool call is actually doing."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        return stringify_command(tool_input.get("command")), tool_input.get("description")
    for key in ("file_path", "path", "notebook_path", "pattern", "query", "url"):
        if tool_input.get(key):
            return str(tool_input[key]), None
    if not tool_input:
        return "", None
    try:
        return compact(json.dumps(tool_input, ensure_ascii=False), 400), None
    except (TypeError, ValueError):
        return compact(str(tool_input), 400), None


