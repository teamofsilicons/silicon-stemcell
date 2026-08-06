"""
core/diagnostics.py

Per-run diagnostic instrumentation tracer for the Silicon autonomous agent
platform.

The v2 tracer is wired through Interface ingestion, manager/provider turns,
tools, replies, and worker linkage. It keeps a durable local JSONL trace plus a
SQLite rollup containing the complete graph for Glass upload.

Design invariants (memo Section 5.1):
    * Fail-open. No failure inside this module may interrupt, delay, or
      terminate a Silicon run. Every public entry point is wrapped so that a
      diagnostics-internal exception is logged and swallowed; the caller's
      control flow is never altered. The ONE exception that is allowed to
      propagate is an exception raised by the *caller's own code* inside a
      `with trace.span(...)` block -- that is the agent's exception, not ours,
      and suppressing it would change agent behaviour.
    * Thread isolation via contextvars. The open-span stack lives in a
      ContextVar, so concurrent carbons running in a ThreadPoolExecutor never
      share span state. No module-global mutable span state exists.
    * Append-only JSONL per run. Events are flushed as they happen, so a run
      that is killed mid-flight (including os.execv re-exec from
      restart_silicon_service) still leaves a readable partial trace.
    * Overhead target < 1ms per span: two monotonic clock reads + a dict
      allocation on the hot path; the JSONL append is a single buffered write.

Schema: "silicon.diag", version 2. Version 2 adds message correlation and the
complete span/event graph to the rollup sent to Glass.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import platform
import sqlite3
import sys
import threading
import time
import traceback
import uuid

from core.diag_retention import maybe_prune
from core.runtime_paths import CODE_ROOT, DATA_ROOT, resolve_data_relative

log = logging.getLogger("silicon.diagnostics")

SCHEMA = "silicon.diag"
VERSION = 2

# Default location, consistent with the already-git-ignored runtime state dir
# (memo Section 5.2). Overridable via env or configure() for tests / relocation.
DEFAULT_DIAG_DIR = os.fspath(
    resolve_data_relative(
        os.environ.get("SILICON_DIAG_DIR", "core/interface_state/diagnostics")
    )
)

# Number of spans surfaced in the bottleneck ranking (memo Section 3.5: "top-N").
DEFAULT_BOTTLENECK_TOP_N = 5

# Stay below Glass ingestion limits so one pathological tool/provider stream
# cannot make the entire run impossible to upload. Dropped counts are reported
# in run metadata; evidence loss must never be silent.
MAX_TRACE_SPANS = 9_000
MAX_TRACE_EVENTS = 18_000

# The open-span stack for the *current execution context*. Because each OS

_WAL_SET = set()  # db_paths already switched to WAL this process (WAL persists on the file)
# thread starts with its own contextvars context, concurrent carbon threads get
# independent stacks for free -- this is the "context-variable isolation"
# mandated by the memo. Never replace this with a module-global list.
_SPAN_STACK: contextvars.ContextVar[list] = contextvars.ContextVar(
    "silicon_diag_span_stack", default=None
)


# --- clock seams -------------------------------------------------------------
# Looked up as module globals on every call so tests can monkeypatch them for
# deterministic duration / bottleneck assertions without touching the API.
def _mono_ns() -> int:
    """Monotonic nanoseconds -- used for durations (immune to wall-clock jumps)."""
    return time.monotonic_ns()


def _wall_ms() -> int:
    """Epoch milliseconds -- used for human-facing t_start_ms / t_end_ms."""
    return int(time.time() * 1000)


# --- token usage container ---------------------------------------------------
class _Tokens:
    __slots__ = ("input", "output", "cache_read", "cache_creation")

    def __init__(self):
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_creation = 0

    @property
    def total(self) -> int:
        # Token COUNT total across all categories. (This is a count, not a
        # billing figure -- cost_usd is tracked separately. If finance wants
        # total defined as input+output only, change this one line.)
        return self.input + self.output + self.cache_read + self.cache_creation

    def as_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "total": self.total,
        }


# --- span --------------------------------------------------------------------
class Span:
    """A single timed step. Use as a context manager via ``trace.span(name)``.

    A Span is always returned in a usable state even if the diagnostics layer is
    failing internally, so ``with trace.span(...)`` is guaranteed never to raise
    on our account.
    """

    __slots__ = (
        "name", "span_id", "parent_id", "path",
        "t_start_ms", "t_end_ms", "_mono_start", "_mono_end", "duration_ms",
        "status", "meta", "cost_usd", "_trace", "_closed", "_tokens",
    )

    def __init__(self, trace: "Trace", name: str, parent: "Span | None"):
        self.name = name
        self.span_id = uuid.uuid4().hex
        self.parent_id = parent.span_id if parent else None
        self.path = f"{parent.path}>{name}" if parent else name
        self.t_start_ms = _wall_ms()
        self._mono_start = _mono_ns()
        self.t_end_ms = None
        self._mono_end = None
        self.duration_ms = None
        self.status = "ok"
        self.meta = {}
        self.cost_usd = 0.0
        self._tokens = _Tokens()
        self._trace = trace
        self._closed = False

    # -- token / cost capture (memo Section 4.4) ------------------------------
    def set_tokens(self, input=0, output=0, cache_read=0, cache_creation=0,
                   cost_usd=0.0, provider_duration_ms=None, **extra):
        """Attach normalized provider usage to this (typically provider_call) span.

        Fail-open: bad input is coerced/ignored rather than raised.
        """
        try:
            self._tokens.input += int(input or 0)
            self._tokens.output += int(output or 0)
            self._tokens.cache_read += int(cache_read or 0)
            self._tokens.cache_creation += int(cache_creation or 0)
            self.cost_usd += float(cost_usd or 0.0)
            if provider_duration_ms is not None:
                self.meta["provider_duration_ms"] = int(provider_duration_ms)
            self.meta["tokens"] = self._tokens.as_dict()
            self.meta["cost_usd"] = self.cost_usd
            if extra:
                self.meta.update(extra)
        except Exception:  # pragma: no cover - defensive, fail-open
            log.exception("diagnostics.set_tokens suppressed")
        return self

    def set_meta(self, **kv):
        try:
            self.meta.update(kv)
        except Exception:  # pragma: no cover
            log.exception("diagnostics.set_meta suppressed")
        return self

    # -- context manager ------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Record the caller's exception against this span, then let it propagate
        # (return False). Our own bookkeeping must never raise.
        try:
            if exc_type is not None:
                self.status = "error"
                self.meta.setdefault("error", getattr(exc_type, "__name__", "error"))
                self.meta.setdefault("error_type", getattr(exc_type, "__name__", "error"))
                try:
                    from core.progress import redact_diagnostic_text

                    summary = redact_diagnostic_text(exc, limit=500)
                except Exception:
                    summary = " ".join(str(exc or "").split())[:500]
                if summary:
                    self.meta.setdefault("error_summary", summary)
                frames = []
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                for frame in traceback.extract_tb(tb)[-20:]:
                    try:
                        filename = os.path.relpath(frame.filename, project_root)
                        if filename.startswith(".."):
                            filename = os.path.basename(frame.filename)
                    except Exception:
                        filename = os.path.basename(frame.filename)
                    frames.append({
                        "file": filename,
                        "line": int(frame.lineno),
                        "function": str(frame.name),
                    })
                if frames:
                    self.meta.setdefault("traceback", frames)
        except Exception:  # pragma: no cover
            pass
        try:
            self._trace._end_span(self)
        except Exception:  # pragma: no cover
            log.exception("diagnostics span close suppressed")
        return False  # never swallow the caller's exception

    def _finalize(self):
        if self._closed:
            return
        self._mono_end = _mono_ns()
        self.t_end_ms = _wall_ms()
        self.duration_ms = max(0, (self._mono_end - self._mono_start) // 1_000_000)
        self._closed = True

    def to_event(self) -> dict:
        return {
            "kind": "span_end",
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "path": self.path,
            "t_start_ms": self.t_start_ms,
            "t_end_ms": self.t_end_ms,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "meta": self.meta,
        }


# --- trace -------------------------------------------------------------------
class Trace:
    """One run's worth of diagnostics. Created via ``Diagnostics.start_run``."""

    __slots__ = (
        "run_id", "parent_run_id", "trigger", "carbon_id",
        "room_id", "message_ids", "response_event_ids", "meta", "events",
        "t_start_ms", "_mono_start", "spans", "_workers_spawned",
        "_dropped_spans", "_dropped_events",
        "_jsonl_path", "_db_path", "_closed", "_disabled",
    )

    def __init__(self, run_id, parent_run_id, trigger, carbon_id, base_dir,
                 room_id="", message_ids=None, meta=None):
        self.run_id = run_id
        self.parent_run_id = parent_run_id
        self.trigger = trigger
        self.carbon_id = carbon_id
        self.room_id = str(room_id or "")
        self.message_ids = list(dict.fromkeys(str(v) for v in (message_ids or []) if v))
        self.response_event_ids = []
        self.meta = dict(meta or {})
        self.events = []
        self.t_start_ms = _wall_ms()
        self._mono_start = _mono_ns()
        self.spans = []            # every span, open or closed, for the rollup
        self._workers_spawned = 0
        self._dropped_spans = 0
        self._dropped_events = 0
        self._closed = False
        self._disabled = False     # set if storage init fails -> in-memory only
        self._jsonl_path = None
        self._db_path = None

        try:
            os.makedirs(base_dir, exist_ok=True)
            self._jsonl_path = os.path.join(base_dir, f"{run_id}.jsonl")
            self._db_path = os.path.join(base_dir, "rollups.sqlite")
            self._append_jsonl({
                "kind": "run_start",
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "trigger": trigger,
                "carbon_id": carbon_id,
                "room_id": self.room_id,
                "message_ids": self.message_ids,
                "meta": self.meta,
                "t_start_ms": self.t_start_ms,
                "schema": SCHEMA,
                "version": VERSION,
            })
        except Exception:
            # Storage unavailable -> degrade to in-memory tracing, never raise.
            log.exception("diagnostics storage init failed; tracing in-memory only")
            self._disabled = True

    def add_message(self, event_id: str, room_id: str = ""):
        """Attach an inbound Glass event to this execution graph."""
        try:
            event_id = str(event_id or "")
            if event_id and event_id not in self.message_ids:
                self.message_ids.append(event_id)
            if room_id and not self.room_id:
                self.room_id = str(room_id)
        except Exception:  # pragma: no cover - fail-open
            pass
        return self

    def add_response(self, event_id: str, **meta):
        """Attach an outbound Glass event produced by this run.

        ``meta`` records the delivery boundary (recipient type/id and room).
        Glass uses that to distinguish a final Carbon response from a
        Silicon-to-Silicon handoff without inspecting message content.
        """
        try:
            event_id = str(event_id or "")
            if event_id and event_id not in self.response_event_ids:
                self.response_event_ids.append(event_id)
                self.event("message.egress", event_id=event_id, **meta)
        except Exception:  # pragma: no cover - fail-open
            pass
        return self

    def event(self, name: str, **meta):
        """Record an instantaneous graph node, such as ingress or progress."""
        try:
            if len(self.events) >= MAX_TRACE_EVENTS:
                self._dropped_events += 1
                return None
            stack = _SPAN_STACK.get() or []
            parent = stack[-1] if stack else None
            item = {
                "event_id": uuid.uuid4().hex,
                "parent_id": parent.span_id if parent else None,
                "name": str(name),
                "t_ms": _wall_ms(),
                "meta": meta,
            }
            self.events.append(item)
            self._append_jsonl({"kind": "event", **item})
            return item
        except Exception:  # pragma: no cover - fail-open
            log.exception("diagnostics.event suppressed")
            return None

    # -- span lifecycle -------------------------------------------------------
    def span(self, name: str) -> Span:
        """Open a child span under whatever span is currently active in THIS
        execution context. Always returns a usable Span; never raises."""
        try:
            if len(self.spans) >= MAX_TRACE_SPANS:
                self._dropped_spans += 1
                return Span(self, name, None)
            stack = _SPAN_STACK.get()
            if stack is None:
                stack = []
                _SPAN_STACK.set(stack)
            parent = stack[-1] if stack else None
            sp = Span(self, name, parent)
            stack.append(sp)
            self.spans.append(sp)
            self._append_jsonl({
                "kind": "span_start",
                "span_id": sp.span_id,
                "parent_id": sp.parent_id,
                "name": sp.name,
                "path": sp.path,
                "t_start_ms": sp.t_start_ms,
            })
            return sp
        except Exception:  # pragma: no cover - fail-open
            log.exception("diagnostics.span suppressed; returning detached span")
            # Detached span: still a valid context manager, just not recorded.
            return Span(self, name, None)

    def _end_span(self, sp: Span):
        sp._finalize()
        # Pop from this context's stack (defensive: only if it's on top).
        try:
            stack = _SPAN_STACK.get()
            if stack and stack[-1] is sp:
                stack.pop()
            elif stack and sp in stack:
                stack.remove(sp)
        except Exception:  # pragma: no cover
            pass
        if sp in self.spans:
            self._append_jsonl(sp.to_event())

    def note_worker_spawned(self, count: int = 1):
        """Phase 3 hook: workers become independent child runs (memo 3.4); the
        originating run records only how many it spawned."""
        try:
            self._workers_spawned += int(count)
        except Exception:  # pragma: no cover
            pass

    def mark_failed(self, reason="process terminated", category="runtime"):
        """Attach a bounded terminal failure marker before a forced close."""
        try:
            try:
                from core.progress import redact_diagnostic_text

                summary = redact_diagnostic_text(
                    reason or "process terminated",
                    limit=500,
                )
            except Exception:
                summary = " ".join(str(reason or "process terminated").split())[:500]
            self.meta["terminal_failure"] = {
                "category": str(category or "runtime")[:80],
                "summary": summary,
            }
            self.event(
                "run.failed",
                category=str(category or "runtime")[:80],
                error_summary=summary,
            )
        except Exception:  # pragma: no cover - fail-open
            pass
        return self

    # -- close / rollup -------------------------------------------------------
    def close(self) -> dict:
        """Close the run, compute + persist the rollup, return it.

        Guaranteed to run to completion and return a dict even if spans were
        left open by an exception, or if storage is unavailable.
        """
        if self._closed:
            return self._build_rollup()  # idempotent-ish
        try:
            # Force-close any spans still open (e.g. abrupt unwinding).
            for sp in self.spans:
                if not sp._closed:
                    sp._finalize()
                    if sp.status == "ok":
                        sp.status = "incomplete"
                    self._append_jsonl(sp.to_event())
            rollup = self._build_rollup()
            self._append_jsonl({"kind": "run_close", "run_id": self.run_id,
                                "rollup": rollup})
            self._write_rollup_row(rollup)
            # Diagnostic evidence is retained indefinitely by default.
            # maybe_prune remains an explicit finite-policy compatibility hook.
            if self._db_path:
                maybe_prune(base_dir=os.path.dirname(self._db_path))
            return rollup
        except Exception:
            log.exception("diagnostics.close suppressed")
            try:
                return self._build_rollup()
            except Exception:  # pragma: no cover
                return self._minimal_rollup()
        finally:
            self._closed = True
            # Clear this context's stack so a pooled thread starts clean.
            try:
                _SPAN_STACK.set([])
            except Exception:  # pragma: no cover
                pass

    def _build_rollup(self) -> dict:
        mono_end = _mono_ns()
        duration_ms = max(0, (mono_end - self._mono_start) // 1_000_000)
        t_end_ms = _wall_ms()

        tokens = _Tokens()
        cost_usd = 0.0
        provider_calls = 0
        rounds = 0
        status = "ok"

        for sp in self.spans:
            if sp.name == "provider_call":
                provider_calls += 1
                tokens.input += sp._tokens.input
                tokens.output += sp._tokens.output
                tokens.cache_read += sp._tokens.cache_read
                tokens.cache_creation += sp._tokens.cache_creation
                cost_usd += sp.cost_usd
            if sp.name.startswith("round["):
                rounds += 1
            if sp.status == "error":
                status = "error"

        if self.meta.get("terminal_failure"):
            status = "error"

        bottlenecks = self._rank_bottlenecks(duration_ms)

        meta = dict(self.meta)
        if self._dropped_spans or self._dropped_events:
            meta["diagnostics_evidence"] = {
                "truncated": True,
                "dropped_spans": self._dropped_spans,
                "dropped_events": self._dropped_events,
                "span_limit": MAX_TRACE_SPANS,
                "event_limit": MAX_TRACE_EVENTS,
            }

        return {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "trigger": self.trigger,
            "carbon_id": self.carbon_id,
            "room_id": self.room_id,
            "message_ids": list(self.message_ids),
            "response_event_ids": list(self.response_event_ids),
            "meta": meta,
            "t_start_ms": self.t_start_ms,
            "t_end_ms": t_end_ms,
            "duration_ms": duration_ms,
            "rounds": rounds,
            "tokens": tokens.as_dict(),
            "cost_usd": round(cost_usd, 6),
            "provider_calls": provider_calls,
            "workers_spawned": self._workers_spawned,
            "bottlenecks": bottlenecks,
            "spans": [sp.to_event() for sp in self.spans],
            "events": list(self.events),
            "status": status,
            "schema": SCHEMA,
            "version": VERSION,
        }

    def _rank_bottlenecks(self, total_ms: int, top_n: int = DEFAULT_BOTTLENECK_TOP_N):
        """Top-N spans by absolute wall-clock duration (memo 3.5).

        pct is the span's gross duration as a fraction of total run duration.
        Note: durations are gross (a parent includes its children), matching the
        memo's "absolute duration_ms" wording and the example path
        'round[0]>manager_turn>provider_call'. Each span also still carries its
        own duration in the JSONL if exclusive/self-time analysis is wanted later.
        """
        closed = [s for s in self.spans if s.duration_ms is not None]
        closed.sort(key=lambda s: s.duration_ms, reverse=True)
        out = []
        for s in closed[:top_n]:
            pct = round((s.duration_ms / total_ms) * 100.0, 2) if total_ms > 0 else 0.0
            out.append({"name": s.path, "duration_ms": s.duration_ms, "pct": pct})
        return out

    def _minimal_rollup(self) -> dict:
        meta = dict(self.meta)
        if self._dropped_spans or self._dropped_events:
            meta["diagnostics_evidence"] = {
                "truncated": True,
                "dropped_spans": self._dropped_spans,
                "dropped_events": self._dropped_events,
                "span_limit": MAX_TRACE_SPANS,
                "event_limit": MAX_TRACE_EVENTS,
            }
        return {
            "run_id": self.run_id, "parent_run_id": self.parent_run_id,
            "trigger": self.trigger, "carbon_id": self.carbon_id,
            "room_id": self.room_id, "message_ids": list(self.message_ids),
            "response_event_ids": list(self.response_event_ids),
            "meta": meta,
            "spans": [], "events": list(self.events),
            "status": "error", "schema": SCHEMA, "version": VERSION,
        }

    # -- storage --------------------------------------------------------------
    def _append_jsonl(self, event: dict):
        """Mirror of write_progress_line: append one JSON object per line and
        flush so partial traces survive an interrupted / re-exec'd process."""
        if self._disabled or not self._jsonl_path:
            return
        try:
            line = json.dumps(event, separators=(",", ":"))
            with open(self._jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()  # push to OS so os.execv re-exec keeps the bytes
        except Exception:  # pragma: no cover - fail-open
            log.exception("diagnostics JSONL append suppressed")

    def _write_rollup_row(self, rollup: dict):
        if self._disabled or not self._db_path:
            return
        try:
            conn = sqlite3.connect(self._db_path, timeout=5.0)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                if self._db_path not in _WAL_SET:
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        _WAL_SET.add(self._db_path)
                    except sqlite3.OperationalError:
                        pass
                _ensure_schema(conn)
                t = rollup.get("tokens", {})
                conn.execute(
                    """INSERT OR REPLACE INTO runs (
                        run_id, parent_run_id, trigger, carbon_id, room_id,
                        message_ids, response_event_ids, meta,
                        t_start_ms, t_end_ms, duration_ms, rounds,
                        tokens_input, tokens_output, tokens_cache_read,
                        tokens_cache_creation, tokens_total, cost_usd,
                        provider_calls, workers_spawned, bottlenecks, spans, events,
                        status, schema, version, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rollup["run_id"], rollup.get("parent_run_id"),
                        rollup.get("trigger"), rollup.get("carbon_id"),
                        rollup.get("room_id"),
                        json.dumps(rollup.get("message_ids", [])),
                        json.dumps(rollup.get("response_event_ids", [])),
                        json.dumps(rollup.get("meta", {})),
                        rollup.get("t_start_ms"), rollup.get("t_end_ms"),
                        rollup.get("duration_ms"), rollup.get("rounds"),
                        t.get("input"), t.get("output"), t.get("cache_read"),
                        t.get("cache_creation"), t.get("total"),
                        rollup.get("cost_usd"), rollup.get("provider_calls"),
                        rollup.get("workers_spawned"),
                        json.dumps(rollup.get("bottlenecks", [])),
                        json.dumps(rollup.get("spans", [])),
                        json.dumps(rollup.get("events", [])),
                        rollup.get("status"), rollup.get("schema"),
                        rollup.get("version"), _wall_ms(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:  # pragma: no cover - fail-open
            log.exception("diagnostics rollup write suppressed")


# --- public entry point ------------------------------------------------------
# --- active-run registry (Phase 3) -------------------------------------------
# Lets code far from the manager loop (worker/handler.py) discover the parent
# run for a carbon without threading Trace objects through every signature.
# main.py's _get_trace/_close_trace remain the lifecycle owners; this is a
# read-mostly index. Fail-open like everything else.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_RUNS: dict = {}  # carbon_id -> Trace
_PENDING_CONTEXTS: dict = {}  # carbon_id -> diagnostic envelopes awaiting a run
_RUNTIME_METADATA_CACHE: dict | None = None


def _runtime_metadata():
    """Return a non-secret runtime fingerprint that makes traces reproducible."""
    global _RUNTIME_METADATA_CACHE
    if _RUNTIME_METADATA_CACHE is not None:
        return dict(_RUNTIME_METADATA_CACHE)
    root = os.fspath(CODE_ROOT)
    data_root = os.fspath(DATA_ROOT)

    def file_hash(path):
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(64 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return ""

    def prompt_manifest_hash():
        prompt_root = os.path.join(root, "prompts")
        living_root = os.path.join(data_root, "prompts")
        try:
            paths = {}
            for directory, _subdirs, filenames in os.walk(prompt_root):
                for filename in filenames:
                    if filename.lower().endswith(".md"):
                        path = os.path.join(directory, filename)
                        paths[os.path.relpath(path, prompt_root)] = path
            for relative in ("MEMORY.md", "LORE.md", "CONTACTS.md", "TEAM.md"):
                path = os.path.join(living_root, relative)
                if os.path.isfile(path):
                    paths[relative] = path
            for prefix in ("memory", "advertising"):
                directory_root = os.path.join(living_root, prefix)
                for directory, _subdirs, filenames in os.walk(directory_root):
                    for filename in filenames:
                        if filename.lower().endswith(".md"):
                            path = os.path.join(directory, filename)
                            paths[os.path.relpath(path, living_root)] = path
            digest = hashlib.sha256()
            for relative, path in sorted(paths.items()):
                relative = f"prompts/{relative}".replace(os.sep, "/")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(64 * 1024), b""):
                        digest.update(chunk)
                digest.update(b"\0")
            return digest.hexdigest() if paths else ""
        except OSError:
            return ""

    def load_object(base, filename):
        try:
            with open(os.path.join(base, filename), encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    info = load_object(root, "silicon.info")
    config = load_object(data_root, "silicon.json")
    brain = str(config.get("brain") or "")
    order = config.get("brain_order")
    if not isinstance(order, list):
        order = [brain] if brain else []
    result = {
        "stemcell_version": str(info.get("version") or ""),
        "diagnostics_schema": f"{SCHEMA}/v{VERSION}",
        "brain": brain,
        "brain_order": [str(item) for item in order if item],
        "config_sha256": file_hash(os.path.join(data_root, "silicon.json")),
        "prompt_manifest_sha256": prompt_manifest_hash(),
        "process_id": os.getpid(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.system().lower(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
    }
    _RUNTIME_METADATA_CACHE = result
    return dict(result)

class Diagnostics:
    @classmethod
    def start_run(cls, trigger, carbon_id, parent_run_id=None,
                  run_id=None, base_dir=None, room_id="", message_ids=None,
                  meta=None) -> Trace:
        """Begin a diagnostic run. Never raises; on failure returns a Trace that
        traces in-memory only so callers can use it unconditionally.

        run_id may be supplied so a worker subprocess can be stamped with the
        parent's id (Phase 3); otherwise a fresh UUID is generated.
        """
        rid = run_id or uuid.uuid4().hex
        bdir = base_dir or DEFAULT_DIAG_DIR
        run_meta = dict(meta or {})
        run_meta.setdefault("runtime", _runtime_metadata())
        # Each run starts with a clean span stack in the current context.
        try:
            _SPAN_STACK.set([])
        except Exception:  # pragma: no cover
            pass
        try:
            return Trace(rid, parent_run_id, trigger, carbon_id, bdir,
                         room_id=room_id, message_ids=message_ids, meta=run_meta)
        except Exception:  # pragma: no cover - belt and suspenders
            log.exception("diagnostics.start_run suppressed")
            t = Trace.__new__(Trace)
            t.run_id, t.parent_run_id = rid, parent_run_id
            t.trigger, t.carbon_id = trigger, carbon_id
            t.room_id, t.message_ids = str(room_id or ""), list(message_ids or [])
            t.response_event_ids, t.meta, t.events = [], run_meta, []
            t.t_start_ms, t._mono_start = _wall_ms(), _mono_ns()
            t.spans, t._workers_spawned = [], 0
            t._dropped_spans, t._dropped_events = 0, 0
            t._closed, t._disabled = False, True
            t._jsonl_path, t._db_path = None, None
            return t

    @classmethod
    def register_active(cls, carbon_id, trace) -> None:
        try:
            with _ACTIVE_LOCK:
                _ACTIVE_RUNS[carbon_id] = trace
        except Exception:  # pragma: no cover - fail-open
            log.exception("diagnostics.register_active suppressed")

    @classmethod
    def get_active_run(cls, carbon_id):
        """Return the currently registered Trace for carbon_id, or None."""
        try:
            with _ACTIVE_LOCK:
                return _ACTIVE_RUNS.get(carbon_id)
        except Exception:  # pragma: no cover - fail-open
            return None

    @classmethod
    def unregister_active(cls, carbon_id, trace=None) -> None:
        """Remove the registration; if trace is given, only if it still matches."""
        try:
            with _ACTIVE_LOCK:
                if trace is None or _ACTIVE_RUNS.get(carbon_id) is trace:
                    _ACTIVE_RUNS.pop(carbon_id, None)
        except Exception:  # pragma: no cover - fail-open
            log.exception("diagnostics.unregister_active suppressed")

    @classmethod
    def close_active_runs(cls, reason="process exiting", category="process_exit") -> int:
        """Fail and persist every active run during graceful process shutdown."""
        try:
            with _ACTIVE_LOCK:
                active = list(_ACTIVE_RUNS.values())
                _ACTIVE_RUNS.clear()
        except Exception:  # pragma: no cover - fail-open
            return 0
        closed = 0
        for trace in dict.fromkeys(active):
            try:
                trace.mark_failed(reason, category=category)
                trace.close()
                closed += 1
            except Exception:  # pragma: no cover - fail-open
                log.exception("diagnostics shutdown close suppressed")
        return closed

    @classmethod
    def rename_active(cls, old_id, new_id) -> None:
        try:
            with _ACTIVE_LOCK:
                if old_id in _ACTIVE_RUNS:
                    _ACTIVE_RUNS[new_id] = _ACTIVE_RUNS.pop(old_id)
        except Exception:  # pragma: no cover - fail-open
            log.exception("diagnostics.rename_active suppressed")

    @classmethod
    def register_pending_context(cls, carbon_id, context) -> None:
        """Carry a queued manager handoff into its next manager run.

        This registry is process-local by design: ``core.messages`` queues and
        drains manager handoffs inside the same long-lived Stemcell process.
        The durable queue still contains the same envelope, so a restart before
        delivery simply registers it on the next ``check_manager_messages``.
        """
        try:
            if not isinstance(context, dict):
                return
            with _ACTIVE_LOCK:
                _PENDING_CONTEXTS.setdefault(str(carbon_id), []).append(dict(context))
        except Exception:  # pragma: no cover - fail-open
            log.exception("diagnostics.register_pending_context suppressed")

    @classmethod
    def consume_pending_contexts(cls, carbon_id):
        """Return and clear queued diagnostic envelopes for ``carbon_id``."""
        try:
            with _ACTIVE_LOCK:
                return _PENDING_CONTEXTS.pop(str(carbon_id), [])
        except Exception:  # pragma: no cover - fail-open
            return []

# --- schema ------------------------------------------------------------------
def _ensure_schema(conn: sqlite3.Connection):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            parent_run_id TEXT,
            trigger TEXT,
            carbon_id TEXT,
            room_id TEXT,
            message_ids TEXT,
            response_event_ids TEXT,
            meta TEXT,
            t_start_ms INTEGER,
            t_end_ms INTEGER,
            duration_ms INTEGER,
            rounds INTEGER,
            tokens_input INTEGER,
            tokens_output INTEGER,
            tokens_cache_read INTEGER,
            tokens_cache_creation INTEGER,
            tokens_total INTEGER,
            cost_usd REAL,
            provider_calls INTEGER,
            workers_spawned INTEGER,
            bottlenecks TEXT,
            spans TEXT,
            events TEXT,
            status TEXT,
            schema TEXT,
            version INTEGER,
            created_at INTEGER
        )"""
    )
    # Existing v1 databases are upgraded in place. SQLite lacks ADD COLUMN IF
    # NOT EXISTS, so inspect the schema before applying each additive field.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    for name, sql_type in (
        ("room_id", "TEXT"),
        ("message_ids", "TEXT"),
        ("response_event_ids", "TEXT"),
        ("meta", "TEXT"),
        ("spans", "TEXT"),
        ("events", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {sql_type}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_trigger ON runs(trigger)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at)")


# --- readers (support tests now; feed the Phase 4 CLI later) -----------------
def read_trace(jsonl_path: str) -> dict:
    """Reconstruct a run from its JSONL file. Tolerant of unclosed runs: missing
    span_end or run_close events do not raise; affected spans are marked open."""
    run = {"run_id": None, "spans": {}, "closed": False, "rollup": None}
    if not os.path.exists(jsonl_path):
        return run
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                # A torn final line from a killed process: ignore, keep going.
                continue
            kind = ev.get("kind")
            if kind == "run_start":
                run["run_id"] = ev.get("run_id")
                run["meta"] = ev
            elif kind == "span_start":
                run["spans"][ev["span_id"]] = {
                    "name": ev.get("name"), "path": ev.get("path"),
                    "parent_id": ev.get("parent_id"),
                    "t_start_ms": ev.get("t_start_ms"),
                    "closed": False, "duration_ms": None, "status": "open",
                }
            elif kind == "span_end":
                sp = run["spans"].setdefault(ev["span_id"], {})
                sp.update({
                    "name": ev.get("name"), "path": ev.get("path"),
                    "parent_id": ev.get("parent_id"),
                    "duration_ms": ev.get("duration_ms"),
                    "status": ev.get("status"), "meta": ev.get("meta"),
                    "closed": True,
                })
            elif kind == "run_close":
                run["closed"] = True
                run["rollup"] = ev.get("rollup")
    return run


def query_rollups(db_path: str, last: int = None, run_id: str = None):
    """Minimal read over rollups.sqlite. (The full `main.py diag` CLI with the
    parent_run_id cost join is Phase 4, gated on Q4.)"""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        if run_id:
            cur = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,))
        elif last:
            cur = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (last,))
        else:
            cur = conn.execute("SELECT * FROM runs ORDER BY created_at DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
