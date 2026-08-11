"""
core/diag_retention.py -- optional manual pruning for diagnostic JSONL traces.

Per-run JSONL files are retained indefinitely by default, alongside the local
SQLite rollups and the DiagnosticRun rows stored by Glass. Operators can still
request an explicit finite window when they invoke these helpers manually.

Trigger model:
    Diagnostics.close() calls maybe_prune() after the rollup row is written.
    Under the default indefinite policy that call is a no-op. Supplying a
    finite retention_days value preserves the old throttled cleanup behavior.

Layering:
    * prune_diagnostic_traces() is a PURE function: age cutoff in, deletions
      out. It knows nothing about who calls it. If ops ever wants a systemd
      timer or cron entry instead/in addition, that trigger is a one-liner
      calling this same function -- the logic is tested once, here.
    * maybe_prune() is the thin trigger wrapper: throttle + fail-open. This is
      the only thing Diagnostics.close() should call.

Safety properties:
    * Fail-open: neither function ever raises. Retention must not be able to
      interrupt, delay, or crash a Silicon run. (maybe_prune is on the
      trace-close path.)
    * Throttled: real work at most once per PRUNE_THROTTLE_HOURS, tracked via
      the mtime of a marker file. Every other close is two stat calls.
    * Concurrency-tolerant: concurrent closers (run_all_managers uses
      ThreadPoolExecutor) may race on the marker; the race is benign -- worst
      case two threads scan the same directory once, and os.remove of an
      already-deleted file is caught per-file.
    * Scoped: deletes only files matching *.jsonl directly inside the
      diagnostics directory. rollups.sqlite (and its -wal/-shm), the marker,
      and anything else are never touched. The mtime cutoff makes the
      just-written trace of the closing run structurally un-deletable (its
      mtime is "now").
"""

from __future__ import annotations

import logging
import os
import time

from helpers.paths import STATE_DIR

log = logging.getLogger("silicon.diag.retention")

# ``None`` is the canonical indefinite-retention value.
DIAG_RETENTION_DAYS: int | None = None

# Real prune work happens at most this often; other closes are ~2 stat calls.
PRUNE_THROTTLE_HOURS: float = 6.0

# Matches core/diagnostics.py's default storage location.
DEFAULT_DIAG_DIR = os.fspath(
    STATE_DIR / "diagnostics"
)

_MARKER_NAME = ".last_prune"
_DAY_SECONDS = 86_400.0


def prune_diagnostic_traces(
    *,
    retention_days: int | None = DIAG_RETENTION_DAYS,
    base_dir: str | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> dict:
    """
    Delete per-run JSONL trace files whose mtime is older than retention_days.
    ``None`` retains every trace and returns a no-op summary.

    Pure and trigger-agnostic: safe to call from Diagnostics.close() (via
    maybe_prune), a shell one-liner, a systemd timer, or a test. Only *.jsonl
    files directly inside the diagnostics directory are candidates.

    Returns a summary dict. Never raises (fail-open).
    """
    summary = {
        "retention_days": retention_days,
        "scanned": 0,
        "deleted": 0,
        "bytes_freed": 0,
        "errors": 0,
        "dry_run": dry_run,
    }
    if retention_days is None:
        return summary
    try:
        now = time.time() if now is None else now
        diag_dir = base_dir if base_dir is not None else DEFAULT_DIAG_DIR
        if not os.path.isdir(diag_dir):
            return summary

        cutoff = now - retention_days * _DAY_SECONDS

        with os.scandir(diag_dir) as entries:
            for entry in entries:
                try:
                    if not entry.name.endswith(".jsonl") or not entry.is_file(
                        follow_symlinks=False
                    ):
                        continue
                    summary["scanned"] += 1
                    st = entry.stat(follow_symlinks=False)
                    if st.st_mtime >= cutoff:
                        continue
                    if dry_run:
                        summary["deleted"] += 1
                        summary["bytes_freed"] += st.st_size
                        log.info(
                            "diag retention (dry-run): would delete %s", entry.path
                        )
                        continue
                    os.remove(entry.path)
                    summary["deleted"] += 1
                    summary["bytes_freed"] += st.st_size
                except OSError as exc:
                    summary["errors"] += 1
                    log.warning(
                        "diag retention: skipping %s: %s", entry.path, exc
                    )

        if summary["deleted"]:
            log.info(
                "diag retention: removed %d trace file(s) older than %dd "
                "(%d bytes)%s",
                summary["deleted"],
                retention_days,
                summary["bytes_freed"],
                " [dry-run]" if dry_run else "",
            )
        return summary
    except Exception as exc:
        log.warning("diag retention: sweep suppressed error: %s", exc)
        return summary


def maybe_prune(
    *,
    base_dir: str | None = None,
    retention_days: int | None = DIAG_RETENTION_DAYS,
    throttle_hours: float = PRUNE_THROTTLE_HOURS,
    now: float | None = None,
) -> dict | None:
    """
    Throttled trigger for Diagnostics.close(). Indefinite retention is a no-op.
    With an explicit finite policy, runs a real sweep at most once per
    throttle_hours; otherwise returns None after two stat calls.

    Never raises (fail-open).
    """
    if retention_days is None:
        return None
    try:
        now = time.time() if now is None else now
        diag_dir = base_dir if base_dir is not None else DEFAULT_DIAG_DIR
        if not os.path.isdir(diag_dir):
            return None

        marker = os.path.join(diag_dir, _MARKER_NAME)
        try:
            if now - os.path.getmtime(marker) < throttle_hours * 3600.0:
                return None
        except OSError:
            pass

        try:
            with open(marker, "a"):
                pass
            os.utime(marker, (now, now))
        except OSError as exc:
            log.warning(
                "diag retention: marker %s unwritable (%s); "
                "pruning will run unthrottled", marker, exc
            )

        return prune_diagnostic_traces(
            retention_days=retention_days,
            base_dir=base_dir,
            now=now,
        )
    except Exception as exc:
        log.warning("diag retention: maybe_prune suppressed error: %s", exc)
        return None
