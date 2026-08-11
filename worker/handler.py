"""Worker process lifecycle: spawn, message, poll, stop, archive.

Workers are subprocesses (browser, terminal, writer) driven by a provider CLI.
This module tracks them in ``_active_workers.json``, serialises browser starts
through a queue so profiles are not shared concurrently, parses provider
output into results, and archives finished runs for later reading.
"""
import ast
import json
import os
import platform
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone

from diagnostics.logs import LOGS_DIR
from helpers.migrate import copy_tree_once
from helpers.paths import CODE_ROOT, DATA_ROOT, STATE_DIR
from helpers.state import file_lock, read_json, update_json, write_json
from inference import (
    STDIN_STREAM,
    STDIN_TASK,
    WorkerLaunchSpec,
    get_provider,
    json_events,
)
from prompts.DNA import get_worker_prompt

IS_WINDOWS = platform.system() == "Windows"

CODE_WORKER_DIR = os.fspath(CODE_ROOT / "worker")
PROJECT_ROOT = os.fspath(DATA_ROOT)
WORKSPACE_ROOT = os.fspath(CODE_ROOT)
WORKER_DIR = os.path.join(PROJECT_ROOT, "worker")
SILICON_CONFIG_FILE = os.path.join(PROJECT_ROOT, "silicon.json")

# `worker/` holds code. What a worker produced is a log, and what Silicon knows
# about its workers is durable state; neither belongs next to the source.
LEGACY_OUTPUTS_DIR = os.path.join(WORKER_DIR, "outputs")
OUTPUTS_DIR = os.fspath(LOGS_DIR / "worker")
WORKER_STATE_DIR = os.fspath(STATE_DIR / "workers")
copy_tree_once(Path(LEGACY_OUTPUTS_DIR), Path(OUTPUTS_DIR))
os.makedirs(OUTPUTS_DIR, exist_ok=True)
os.makedirs(WORKER_STATE_DIR, exist_ok=True)

ACTIVE_FILE = os.path.join(WORKER_STATE_DIR, "_active_workers.json")
BROWSER_QUEUE_FILE = os.path.join(WORKER_STATE_DIR, "_browser_queue.json")
ARCHIVE_META_FILE = os.path.join(WORKER_STATE_DIR, "_archive_meta.json")
WORKER_REGISTRY_FILE = os.path.join(WORKER_STATE_DIR, "_worker_registry.json")
PROFILED_BROWSER_LOCK_FILE = os.path.join(
    WORKER_STATE_DIR,
    ".profiled-browser-launch.json",
)

BROWSER_WORKER_MODEL = "sonnet"
WORKER_PROVIDER_FALLBACKS = {
    "browser": ["claude"],
    "terminal": ["claude"],
    "writer": ["claude"],
}
VALID_WORKER_PROVIDERS = {"claude", "codex", "chatgpt"}

def _legacy_browser_profile():
    """Read the one supported legacy env.py value without executing the file."""

    path = DATA_ROOT / "env.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return "silicon"
    for node in tree.body:
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(getattr(node, "value", None), ast.Constant)
            and isinstance(node.value.value, str)
        ):
            names = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
            if any(
                isinstance(name, ast.Name) and name.id == "BROWSER_PROFILE"
                for name in names
            ):
                return node.value.value
    return "silicon"


_BROWSER_PROFILE = _legacy_browser_profile()
SILICON_BROWSER_PROFILE = _BROWSER_PROFILE


def _worker_process_env(contact_id, worker_id="", worker_type=""):
    """Return the environment for a worker process, carrying its identity.

    The worker gets its own `iwantto` token so commands it runs resolve to the
    worker rather than to the manager that spawned it. ``issue_run_env`` drops
    any inherited actor variables before setting the new ones, so a parent
    manager's identity cannot leak one Carbon's context into another Carbon's
    worker.
    """
    if not worker_id:
        return os.environ.copy()
    from diagnostics.iwantto import journal
    from diagnostics.iwantto.actor import WORKER, issue_run_env

    _token, env = issue_run_env(
        WORKER,
        worker_id,
        contact_id,
        worker_type=worker_type,
    )
    # Called exactly once per launch, so it is the one place that sees every
    # worker run start.
    journal.record_run(
        WORKER,
        worker_id,
        contact_id,
        worker_type=worker_type,
    )
    return env


# Streaming stdin is what makes a running worker reachable. Turn it off with
# SILICON_STREAMING_INPUT=0 and workers fall back to picking mail up on their
# next iwantto command.
def _worker_streaming_enabled():
    return os.environ.get("SILICON_STREAMING_INPUT", "1") != "0" and not IS_WINDOWS


def _start_worker_feeder(worker_id, carbon_id, process, task, output_path):
    """Keep a worker reachable for as long as it is working.

    A worker is a subprocess in the middle of a job — there is no way to push a
    line into it once it has started. Streaming stdin is that way: the task goes
    in first, and anything a manager sends afterwards is written into the same
    live session, which the worker picks up at its next tool boundary instead of
    waiting until it next runs a command.

    The thread closes stdin once the worker reports a result, which is what lets
    it exit. If the Stemcell dies first the pipe closes with it, the worker sees
    end-of-input, and it finishes on its own.
    """
    from diagnostics.iwantto import journal, mailbox
    from inference import stream_json_user

    def write(text):
        try:
            process.stdin.write(stream_json_user(text))
            process.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def pump():
        if not write(task):
            return
        offset = 0
        finished = False
        while process.poll() is None and not finished:
            delivered = False
            for item in mailbox.drain("worker", worker_id):
                sender = str(item.get("from") or "your manager")
                if write(f"[MESSAGE FROM Your Manager]\n{item.get('message') or ''}"):
                    delivered = True
                    try:
                        journal.record_message(
                            "in", carbon_id, via="injected",
                            sender=sender, body=str(item.get("message") or ""),
                        )
                    except Exception:
                        pass
            if delivered:
                continue
            # The worker's own stdout is the only signal that it is done.
            try:
                with open(output_path, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    for line in handle:
                        if '"type":"result"' in line.replace(" ", ""):
                            finished = True
                    offset = handle.tell()
            except OSError:
                pass
            if not finished:
                time.sleep(0.5)
        # Nothing more is coming; let the worker exit.
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    thread = threading.Thread(
        target=pump, name=f"worker-feed-{worker_id}", daemon=True
    )
    thread.start()
    return thread


def _maintenance_reference():
    try:
        from manager.runtime.maintenance import current_activity

        activity = current_activity()
        return activity.reference() if activity is not None else {}
    except Exception:
        return {}


def _maintenance_activity(reference):
    try:
        from manager.runtime.maintenance import activity_from_reference

        return activity_from_reference(reference)
    except Exception:
        return None


def _heartbeat_maintenance_activity(reference):
    activity = _maintenance_activity(reference)
    if activity is None:
        return None
    try:
        from manager.runtime.maintenance import heartbeat_activity

        return activity if heartbeat_activity(activity) else None
    except Exception:
        return None


def _release_maintenance_activity(reference):
    activity = _maintenance_activity(reference)
    if activity is None:
        return False
    try:
        from manager.runtime.maintenance import release_activity

        return release_activity(activity)
    except Exception:
        return False


def reconcile_maintenance_activities():
    """Attach leases to legacy active/queued worker records before draining."""
    try:
        from manager.runtime.maintenance import COORDINATOR
    except Exception:
        return 0

    adopted = 0
    with file_lock(ACTIVE_FILE):
        active = _load_active()
        changed = False
        for worker_id, info in active.items():
            if not isinstance(info, dict):
                continue
            reference = info.get("maintenance_activity") or {}
            if _heartbeat_maintenance_activity(reference) is not None:
                continue
            activity = COORDINATOR.adopt_prefence_activity(
                "worker",
                activity_id=str(worker_id),
                contact_id=str(info.get("carbon_id") or ""),
                started_at=_number_or_zero(info.get("started")),
            )
            if activity is not None:
                info["maintenance_activity"] = activity.reference()
                adopted += 1
                changed = True
        if changed:
            _save_active(active)

    with file_lock(BROWSER_QUEUE_FILE):
        queue = _load_browser_queue()
        changed = False
        for info in queue:
            if not isinstance(info, dict):
                continue
            reference = info.get("maintenance_activity") or {}
            if _heartbeat_maintenance_activity(reference) is not None:
                continue
            activity = COORDINATOR.adopt_prefence_activity(
                "worker",
                activity_id=str(info.get("worker_id") or ""),
                contact_id=str(info.get("carbon_id") or ""),
                started_at=_number_or_zero(info.get("queued_at")),
            )
            if activity is not None:
                info["maintenance_activity"] = activity.reference()
                adopted += 1
                changed = True
        if changed:
            _save_browser_queue(queue)
    return adopted


def _number_or_zero(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --- State persistence ---

def _load_active():
    value = read_json(ACTIVE_FILE, {})
    return value if isinstance(value, dict) else {}


def _save_active(active):
    write_json(ACTIVE_FILE, active)


def _load_browser_queue():
    value = read_json(BROWSER_QUEUE_FILE, [])
    return value if isinstance(value, list) else []


def _save_browser_queue(queue):
    write_json(BROWSER_QUEUE_FILE, queue)


def _load_archive_meta():
    value = read_json(ARCHIVE_META_FILE, {})
    return value if isinstance(value, dict) else {}


def _migrate_worker_record(worker_id, record):
    changed = False

    if "session_uuid" in record and "session_id" not in record:
        record["session_id"] = record.pop("session_uuid")
        changed = True

    if "provider" not in record:
        if record.get("worker_type") in ("browser", "writer"):
            record["provider"] = "claude"
            changed = True
        elif record.get("session_id"):
            record["provider"] = "claude"
            changed = True
    elif record.get("provider") == "chatgpt":
        record["provider"] = "codex"
        changed = True

    if "worker_id" not in record:
        record["worker_id"] = worker_id
        changed = True

    return record, changed


def _load_worker_registry():
    with file_lock(WORKER_REGISTRY_FILE):
        registry = read_json(WORKER_REGISTRY_FILE, {})
        if not isinstance(registry, dict):
            return {}

        changed = False
        for worker_id, record in registry.items():
            _, record_changed = _migrate_worker_record(worker_id, record)
            changed = changed or record_changed

        if changed:
            _save_worker_registry(registry)

        return registry


def _save_worker_registry(registry):
    write_json(WORKER_REGISTRY_FILE, registry)


def _remove_worker_record(worker_id):
    def remove(registry):
        if isinstance(registry, dict):
            registry.pop(worker_id, None)

    update_json(WORKER_REGISTRY_FILE, {}, remove)


def _worker_launch_lock_path(worker_id):
    safe_id = "".join(
        char if char.isalnum() or char in "-_." else "-"
        for char in str(worker_id or "worker")
    )[:80]
    return os.path.join(WORKER_STATE_DIR, f".launch-{safe_id}.json")


def _remove_active_worker(worker_id, expected_run_id="", expected_claim_token=""):
    removed = {}

    def remove(active):
        if not isinstance(active, dict):
            return
        current = active.get(worker_id)
        if not isinstance(current, dict):
            return
        if expected_run_id and str(current.get("run_id") or "") != str(expected_run_id):
            return
        if (
            expected_claim_token
            and str(current.get("_completion_claim_token") or "")
            != str(expected_claim_token)
        ):
            return
        removed.update(current)
        active.pop(worker_id, None)

    update_json(ACTIVE_FILE, {}, remove)
    return removed


def _claim_completed_worker(worker_id, expected_run_id=""):
    claimed = {}
    claim_token = uuid.uuid4().hex
    now = time.time()

    def claim(active):
        if not isinstance(active, dict):
            return
        current = active.get(worker_id)
        if not isinstance(current, dict):
            return
        if expected_run_id and str(current.get("run_id") or "") != str(expected_run_id):
            return
        claimed_at = float(current.get("_completion_claimed_at") or 0)
        if claimed_at and now - claimed_at < 60:
            return
        current["_completion_claimed_at"] = now
        current["_completion_claim_token"] = claim_token
        claimed.update(current)

    update_json(ACTIVE_FILE, {}, claim)
    return claimed


def _utc_timestamp_slug():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _run_output_path(worker_id, run_id):
    return os.path.join(OUTPUTS_DIR, f"{worker_id}-{run_id}.log")


def _make_archive_id(worker_id, run_id):
    return f"{worker_id}-{run_id}"


def _get_worker_record(worker_id):
    return _load_worker_registry().get(worker_id)


def _create_worker_record(worker_id, worker_type, carbon_id, incognito=False):
    now = time.time()
    record = {
        "worker_id": worker_id,
        "worker_type": worker_type,
        "carbon_id": carbon_id,
        "created_at": now,
        "last_used_at": now,
        "last_run_id": "",
        "last_archive_id": "",
        "incognito": incognito,
        "provider": "",
        "session_id": "",
    }
    created = False

    def create(registry):
        nonlocal created
        if not isinstance(registry, dict) or worker_id in registry:
            return
        registry[worker_id] = record
        created = True

    update_json(WORKER_REGISTRY_FILE, {}, create)
    if not created:
        return None, f"Error: Worker '{worker_id}' already exists. Use worker/message to prompt it again."
    return record, ""


def _update_worker_record(worker_id, **updates):
    def update(registry):
        if isinstance(registry, dict) and worker_id in registry:
            registry[worker_id].update(updates)

    update_json(WORKER_REGISTRY_FILE, {}, update)


def _read_silicon_config():
    if not os.path.exists(SILICON_CONFIG_FILE):
        return {}
    try:
        with open(SILICON_CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {}


def _normalize_provider(provider):
    provider = (provider or "").strip().lower()
    if provider == "chatgpt":
        return "codex"
    return provider


def _get_worker_provider_order(worker_type):
    config = _read_silicon_config()
    raw = config.get("workers", {}).get(worker_type)

    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return WORKER_PROVIDER_FALLBACKS.get(worker_type, ["claude"])[:]

    providers = []
    seen = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        provider = _normalize_provider(item)
        if provider in VALID_WORKER_PROVIDERS and provider not in seen:
            seen.add(provider)
            providers.append(provider)

    return providers or WORKER_PROVIDER_FALLBACKS.get(worker_type, ["claude"])[:]


def _worker_provider(provider):
    """The provider object for a worker record, defaulting to Claude."""
    return get_provider(_normalize_provider(provider) or "claude")


def _archive_active_output(worker_id, worker_info, carbon_id):
    output_path = worker_info.get("output_path")
    if not output_path or not os.path.exists(output_path):
        return ""

    run_id = worker_info.get("run_id") or _utc_timestamp_slug()
    archive_id = _make_archive_id(worker_id, run_id)
    archive_path = os.path.join(OUTPUTS_DIR, f"{archive_id}.log")

    if os.path.abspath(output_path) != os.path.abspath(archive_path):
        os.rename(output_path, archive_path)

    archive_record = {
        "worker_id": worker_id,
        "run_id": run_id,
        "provider": worker_info.get("provider", ""),
        "session_id": worker_info.get("session_id", ""),
        "carbon_id": carbon_id,
        "worker_type": worker_info.get("worker_type", "unknown"),
        "task": worker_info.get("task", ""),
        "started_at": worker_info.get("started"),
        "archived_at": time.time(),
        "incognito": worker_info.get("incognito", False),
    }

    def remember(meta):
        if isinstance(meta, dict):
            meta[archive_id] = archive_record

    update_json(ARCHIVE_META_FILE, {}, remember)
    return archive_id


# --- Internal helpers ---

def _is_profiled_browser_active():
    active = _load_active()
    for info in active.values():
        if info.get("worker_type") == "browser" and not info.get("incognito", False):
            return True
    return False


def _get_silicon_browser_socket_dir():
    override = os.environ.get("SILICON_BROWSER_SOCKET_DIR")
    if override:
        return override
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "silicon-browser")
    home = os.path.expanduser("~")
    if home:
        return os.path.join(home, ".silicon-browser")
    return os.path.join("/tmp", "silicon-browser")


def _kill_incognito_daemon_by_pid(worker_id):
    socket_dir = _get_silicon_browser_socket_dir()
    pid_file = os.path.join(socket_dir, f"incognito-{worker_id}.pid")
    if not os.path.exists(pid_file):
        return
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    for ext in (".pid", ".sock", ".stream"):
        try:
            fpath = os.path.join(socket_dir, f"incognito-{worker_id}{ext}")
            if os.path.exists(fpath):
                os.unlink(fpath)
        except Exception:
            pass


def _cleanup_silicon_browser_session(worker_id, worker_info):
    if worker_info.get("worker_type") != "browser":
        return
    if worker_info.get("incognito", False):
        try:
            result = subprocess.run(
                ["silicon-browser", "--session", f"incognito-{worker_id}", "close"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                _kill_incognito_daemon_by_pid(worker_id)
        except Exception:
            _kill_incognito_daemon_by_pid(worker_id)


def sweep_orphaned_daemons():
    socket_dir = _get_silicon_browser_socket_dir()
    if not os.path.exists(socket_dir):
        return []

    active = _load_active()
    active_worker_ids = set(active.keys())
    cleaned = []

    for fname in os.listdir(socket_dir):
        if not fname.startswith("incognito-") or not fname.endswith(".pid"):
            continue
        worker_id = fname[len("incognito-"):-len(".pid")]
        if worker_id not in active_worker_ids:
            _kill_incognito_daemon_by_pid(worker_id)
            cleaned.append(worker_id)

    return cleaned


def _get_popen_kwargs(env, output_file, stdin=None):
    popen_kwargs = dict(
        stdout=output_file,
        stderr=subprocess.PIPE,
        env=env,
        **({"stdin": stdin} if stdin is not None else {}),
        text=True,
        # Workers execute against the same active source generation as the
        # manager. Relative self-edits are therefore live, restartable, and
        # captured by updater/backup customization overlays. Runtime state
        # continues to use the explicit DATA_ROOT paths above.
        cwd=WORKSPACE_ROOT,
    )
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["preexec_fn"] = os.setsid
    return popen_kwargs


def _terminate_process(process):
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:
        pass


def _read_text_file(path):
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return ""


def _sync_session_id(worker_id, worker_info=None):
    """Remember a session id the provider only revealed in its own output."""
    if worker_info is None:
        worker_info = _load_active().get(worker_id)
    if not worker_info:
        return ""

    if worker_info.get("session_id"):
        return worker_info["session_id"]

    raw = _read_text_file(worker_info.get("output_path"))
    session_id = _worker_provider(worker_info.get("provider")).session_id_from_output(raw)
    if not session_id:
        return ""

    def remember_session(active):
        if isinstance(active, dict) and worker_id in active:
            active[worker_id]["session_id"] = session_id

    update_json(ACTIVE_FILE, {}, remember_session)

    _update_worker_record(worker_id, session_id=session_id)
    return session_id


def _wait_for_session_id(provider, process, output_path, timeout_seconds=20.0):
    """Block until the provider names its session, or explain why it never did."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        raw = _read_text_file(output_path)
        session_id = provider.session_id_from_output(raw)
        if session_id:
            return session_id, ""

        returncode = process.poll()
        if returncode is not None:
            stderr = ""
            try:
                if process.stderr:
                    stderr = process.stderr.read().strip()
            except Exception:
                stderr = ""
            raw_tail = raw.strip().splitlines()[-1] if raw.strip() else ""
            detail = stderr or raw_tail or f"process exited with code {returncode}"
            return "", detail

        time.sleep(0.1)

    return "", f"Timed out waiting for {provider.name} session id"


def _record_active_run(worker_id, provider, session_id, process, task, worker_type, carbon_id, output_path, incognito, run_id):
    diag_parent_run_id = ""
    diag_room_id = ""
    diag_message_ids = []
    try:
        from diagnostics.store import Diagnostics
        parent_trace = Diagnostics.get_active_run(carbon_id)
        if parent_trace:
            diag_parent_run_id = parent_trace.run_id
            diag_room_id = parent_trace.room_id
            diag_message_ids = list(parent_trace.message_ids)
    except Exception:
        pass
    active_record = {
        "pid": process.pid,
        "started": time.time(),
        "task": task,
        "worker_type": worker_type,
        "carbon_id": carbon_id,
        "output_path": output_path,
        "incognito": incognito,
        "provider": provider,
        "session_id": session_id,
        "run_id": run_id,
        "diag_parent_run_id": diag_parent_run_id,
        "diag_room_id": diag_room_id,
        "diag_message_ids": diag_message_ids,
        "maintenance_activity": _maintenance_reference(),
    }

    def remember_active(active):
        if isinstance(active, dict):
            active[worker_id] = active_record

    update_json(ACTIVE_FILE, {}, remember_active)
    _update_worker_record(
        worker_id,
        provider=provider,
        session_id=session_id,
        last_used_at=time.time(),
        last_run_id=run_id,
        incognito=incognito,
    )


def _browser_session_env(env, worker_type, worker_id, incognito):
    """Point a browser worker at the right session and profile."""
    if worker_type != "browser":
        return env
    if incognito:
        # Ephemeral: fresh session, no shared profile/cookies.
        env["SILICON_BROWSER_SESSION"] = f"incognito-{worker_id}"
        env.pop("SILICON_BROWSER_PROFILE", None)
    else:
        # Shared browser: same live session AND the persistent profile, so
        # logins saved by `share`/`close` are loaded back. silicon-browser
        # reads both env vars as defaults for --session/--profile.
        env["SILICON_BROWSER_SESSION"] = SILICON_BROWSER_PROFILE
        env["SILICON_BROWSER_PROFILE"] = SILICON_BROWSER_PROFILE
    return env


def _launch_worker_process(worker_id, task, worker_type, carbon_id, incognito=False, resume=False, provider=None, session_id=""):
    """Start a detached worker on one provider.

    The provider builds its own command and knows how to read its own output;
    everything here — the registry, the environment, the output file — is the
    same whichever one answers.
    """
    engine = _worker_provider(provider or "claude")

    worker_record = _get_worker_record(worker_id)
    if not worker_record:
        return False, f"Error: Worker '{worker_id}' is not registered."

    system_prompt, err = get_worker_prompt(worker_type)
    if err:
        return False, err

    if not session_id:
        session_id = worker_record.get("session_id", "")
    if engine.mints_own_session_id:
        if resume and not session_id:
            return False, (
                f"Error: Worker '{worker_id}' has no saved {engine.name} "
                "session id to resume."
            )
    elif not session_id:
        session_id = str(uuid.uuid4())

    run_id = _utc_timestamp_slug()
    output_path = _run_output_path(worker_id, run_id)
    os.makedirs(OUTPUTS_DIR, exist_ok=True)

    command = engine.worker_command(WorkerLaunchSpec(
        worker_id=worker_id,
        worker_type=worker_type,
        task=task,
        system_prompt=system_prompt,
        session_id=session_id,
        resume=resume,
        streaming=_worker_streaming_enabled(),
        cwd=WORKSPACE_ROOT,
        scratch_dir=WORKER_STATE_DIR,
        model=BROWSER_WORKER_MODEL if worker_type == "browser" else "",
    ))

    env = _browser_session_env(
        _worker_process_env(carbon_id, worker_id, worker_type),
        worker_type,
        worker_id,
        incognito,
    )

    output_file = open(output_path, "w", encoding="utf-8")
    try:
        popen_kwargs = _get_popen_kwargs(env, output_file, stdin=command.popen_stdin())
        process = subprocess.Popen(command.argv, **popen_kwargs)
        if command.stdin == STDIN_TASK:
            try:
                process.stdin.write(task)
                process.stdin.close()
            except BrokenPipeError:
                pass
    except Exception as e:
        output_file.close()
        return False, f"{engine.name.capitalize()} launch failed: {e}"
    finally:
        output_file.close()

    if command.stdin == STDIN_STREAM:
        _start_worker_feeder(worker_id, carbon_id, process, task, output_path)

    if command.captures_session_id:
        captured, detail = _wait_for_session_id(engine, process, output_path)
        if not captured:
            _terminate_process(process)
            return False, f"{engine.name.capitalize()} launch failed: {detail}"
        session_id = captured

    _record_active_run(worker_id, engine.name, session_id, process, task, worker_type, carbon_id, output_path, incognito, run_id)
    # Profile vs incognito only means anything for a browser.
    mode = ("incognito" if incognito else "profiled") if worker_type == "browser" else ""
    descriptor = ", ".join(part for part in (worker_type, mode, engine.name) if part)
    return True, f"Done. Worker '{worker_id}' ({descriptor}) started (pid: {process.pid}, run: {run_id})"


def _process_browser_queue():
    # Keep the profiled-browser availability check, dequeue, and launch in one
    # cross-process transaction. Otherwise two manager threads can both see an
    # idle profile and launch competing sessions.
    with file_lock(PROFILED_BROWSER_LOCK_FILE), file_lock(BROWSER_QUEUE_FILE):
        if _is_profiled_browser_active():
            return None, None

        queue = _load_browser_queue()
        if not queue:
            return None, None

        next_job = queue[0]
        activity = _heartbeat_maintenance_activity(
            next_job.get("maintenance_activity")
        )
        try:
            from manager.runtime.maintenance import (
                acquire_descendant_activity,
                bind_activity,
                public_status,
            )

            phase = public_status().get("phase")
        except Exception:
            acquire_descendant_activity = None
            bind_activity = None
            phase = "available"

        # A legacy/unleased queued job cannot cross a newly raised fence.  It
        # remains durable and will be admitted when the runtime is available.
        if activity is None and phase != "available":
            return None, None
        if activity is None and acquire_descendant_activity is not None:
            activity = acquire_descendant_activity(
                "worker",
                activity_id=str(next_job.get("worker_id") or ""),
                contact_id=str(next_job.get("carbon_id") or ""),
            )
            if activity is None:
                return None, None
            next_job["maintenance_activity"] = activity.reference()

        queue.pop(0)
        _save_browser_queue(queue)

        scope = bind_activity(activity) if bind_activity is not None else None
        if scope is not None:
            scope.__enter__()
        try:
            if next_job.get("resume"):
                ok, result = _launch_worker_process(
                    next_job["worker_id"],
                    next_job["task"],
                    "browser",
                    next_job.get("carbon_id", "unknown"),
                    incognito=next_job.get("incognito", False),
                    resume=True,
                    provider=next_job.get("provider", "claude"),
                    session_id=next_job.get("session_id", ""),
                )
            else:
                ok, result = _launch_with_provider_order(
                    next_job["worker_id"],
                    next_job["task"],
                    "browser",
                    next_job.get("carbon_id", "unknown"),
                    incognito=next_job.get("incognito", False),
                    providers=next_job.get("providers"),
                )
        finally:
            if scope is not None:
                scope.__exit__(None, None, None)
        if not ok and activity is not None:
            try:
                from manager.runtime.maintenance import release_activity

                release_activity(activity)
            except Exception:
                pass
    try:
        from interface.work_updates import record_worker_state

        record_worker_state(
            next_job.get("carbon_id", "unknown"),
            next_job["worker_id"],
            "in_progress" if ok else "failed",
            "Browser worker is running" if ok else "Browser worker failed to launch",
        )
    except Exception:
        pass
    outcome = "Dequeued and started" if ok else "Dequeued but failed to start"
    return f"[Browser Queue] {outcome}: {result}", next_job.get("carbon_id", "unknown")


# --- Public API: starting workers ---

def _launch_with_provider_order(worker_id, task, worker_type, carbon_id, incognito=False, providers=None):
    worker_record = _get_worker_record(worker_id)
    if not worker_record:
        return False, f"Error: Worker '{worker_id}' is not registered."

    providers = providers or _get_worker_provider_order(worker_type)
    errors = []

    for provider in providers:
        provider = _normalize_provider(provider)
        session_id = worker_record.get("session_id", "") if _normalize_provider(worker_record.get("provider")) == provider else ""
        ok, result = _launch_worker_process(
            worker_id,
            task,
            worker_type,
            carbon_id,
            incognito=incognito,
            resume=False,
            provider=provider,
            session_id=session_id,
        )
        if ok:
            return True, result
        errors.append(f"{provider}: {result}")
        if not _is_provider_launch_failure(result):
            return False, result

    return False, "Error: Could not start worker. " + " | ".join(errors)


def _is_provider_launch_failure(result):
    text = (result or "").strip()
    return text.startswith("Claude launch failed:") or text.startswith("Codex launch failed:")


def start_browser_worker(worker_id, task, carbon_id, incognito=False, resume=False):
    if incognito:
        with file_lock(_worker_launch_lock_path(worker_id)):
            if worker_id in _load_active():
                return f"Error: Worker '{worker_id}' is already active."
            worker_record = _get_worker_record(worker_id)
            session_id = worker_record.get("session_id", "") if worker_record else ""
            provider = _normalize_provider(worker_record.get("provider", "")) if worker_record else ""
            if resume:
                _, result = _launch_worker_process(
                    worker_id, task, "browser", carbon_id, incognito=True, resume=True, provider=provider or "claude", session_id=session_id
                )
            else:
                _, result = _launch_with_provider_order(worker_id, task, "browser", carbon_id, incognito=True)
            return result

    with file_lock(PROFILED_BROWSER_LOCK_FILE), file_lock(BROWSER_QUEUE_FILE):
        active = _load_active()
        if worker_id in active:
            return f"Error: Worker '{worker_id}' is already active."

        worker_record = _get_worker_record(worker_id)
        session_id = worker_record.get("session_id", "") if worker_record else ""
        provider = _normalize_provider(worker_record.get("provider", "")) if worker_record else ""

        queue = _load_browser_queue()
        if any(q["worker_id"] == worker_id for q in queue):
            return f"Error: Worker '{worker_id}' is already in the browser queue."

        if not _is_profiled_browser_active():
            if resume:
                _, result = _launch_worker_process(
                    worker_id, task, "browser", carbon_id, incognito=False, resume=True, provider=provider or "claude", session_id=session_id
                )
            else:
                _, result = _launch_with_provider_order(worker_id, task, "browser", carbon_id, incognito=False)
            return result

        providers = [_normalize_provider(provider)] if resume and provider else _get_worker_provider_order("browser")
        queue.append({
            "worker_id": worker_id,
            "task": task,
            "carbon_id": carbon_id,
            "queued_at": time.time(),
            "incognito": False,
            "resume": resume,
            "session_id": session_id,
            "provider": provider,
            "providers": providers,
            "maintenance_activity": _maintenance_reference(),
        })
        _save_browser_queue(queue)

        position = len(queue)
        action = "resume" if resume else "start"
        return f"Done. Worker '{worker_id}' (browser) queued at position {position}. Will {action} when current profiled browser worker finishes."


def start_terminal_worker(worker_id, task, carbon_id, resume=False):
    with file_lock(_worker_launch_lock_path(worker_id)):
        active = _load_active()
        if worker_id in active:
            return f"Error: Worker '{worker_id}' is already active."

        worker_record = _get_worker_record(worker_id)
        if not worker_record:
            return f"Error: Worker '{worker_id}' is not registered."

        if resume:
            provider = _normalize_provider(worker_record.get("provider", "claude"))
            session_id = worker_record.get("session_id", "")
            _ok, result = _launch_worker_process(
                worker_id,
                task,
                "terminal",
                carbon_id,
                resume=True,
                provider=provider,
                session_id=session_id,
            )
            return result

        ok, result = _launch_with_provider_order(worker_id, task, "terminal", carbon_id)
    if ok:
        return result

    _remove_worker_record(worker_id)
    return result


def start_writer_worker(worker_id, task, carbon_id, resume=False):
    with file_lock(_worker_launch_lock_path(worker_id)):
        active = _load_active()
        if worker_id in active:
            return f"Error: Worker '{worker_id}' is already active."

        worker_record = _get_worker_record(worker_id)
        session_id = worker_record.get("session_id", "") if worker_record else ""
        provider = _normalize_provider(worker_record.get("provider", "")) if worker_record else ""
        if resume:
            _, result = _launch_worker_process(
                worker_id, task, "writer", carbon_id, resume=True, provider=provider or "claude", session_id=session_id
            )
        else:
            _, result = _launch_with_provider_order(worker_id, task, "writer", carbon_id)
        return result


def start_worker(worker_id, task, worker_type, carbon_id, incognito=False):
    if not worker_type:
        return "Error: worker_type is required. Available types: browser, terminal, writer"

    worker_type = worker_type.lower()
    try:
        from manager.runtime.maintenance import (
            acquire_descendant_activity,
            bind_activity,
            release_activity,
        )

        activity = acquire_descendant_activity(
            "worker",
            activity_id=worker_id,
            contact_id=carbon_id,
        )
    except Exception:
        activity = None
        bind_activity = None
        release_activity = None
    if activity is None:
        return (
            "Error: Silicon is preparing an update. New workers are paused; "
            "the manager task will resume after maintenance."
        )

    result = ""
    scope = bind_activity(activity)
    scope.__enter__()
    try:
        _, err = _create_worker_record(
            worker_id,
            worker_type,
            carbon_id,
            incognito=incognito,
        )
        if err:
            result = err
        elif worker_type == "browser":
            result = start_browser_worker(
                worker_id,
                task,
                carbon_id,
                incognito=incognito,
                resume=False,
            )
            if result.startswith("Error:"):
                _remove_worker_record(worker_id)
        elif worker_type == "terminal":
            result = start_terminal_worker(
                worker_id,
                task,
                carbon_id,
                resume=False,
            )
        elif worker_type == "writer":
            result = start_writer_worker(
                worker_id,
                task,
                carbon_id,
                resume=False,
            )
            if result.startswith("Error:"):
                _remove_worker_record(worker_id)
        else:
            _remove_worker_record(worker_id)
            result = (
                f"Error: invalid worker_type '{worker_type}'. "
                "Available types: browser, terminal, writer"
            )
    finally:
        scope.__exit__(None, None, None)

    if result.startswith("Error:") and release_activity is not None:
        release_activity(activity)
    return result


def message_worker(worker_id, task, carbon_id):
    worker_record = _get_worker_record(worker_id)
    if not worker_record:
        return f"Error: Worker '{worker_id}' does not exist. Create it first with worker/new."
    if worker_record.get("carbon_id") != carbon_id:
        return f"Error: Worker '{worker_id}' does not belong to you."

    worker_type = worker_record.get("worker_type", "").lower()
    incognito = worker_record.get("incognito", False)

    active = _load_active()
    if worker_id in active:
        return f"Error: Worker '{worker_id}' is already active."

    queue = _load_browser_queue()
    if any(q["worker_id"] == worker_id for q in queue):
        return f"Error: Worker '{worker_id}' is already in the browser queue."

    try:
        from manager.runtime.maintenance import (
            acquire_descendant_activity,
            bind_activity,
            release_activity,
        )

        activity = acquire_descendant_activity(
            "worker",
            activity_id=worker_id,
            contact_id=carbon_id,
        )
    except Exception:
        activity = None
        bind_activity = None
        release_activity = None
    if activity is None:
        return (
            "Error: Silicon is preparing an update. Worker resumes are paused "
            "until maintenance finishes."
        )

    scope = bind_activity(activity)
    scope.__enter__()
    try:
        if worker_type == "browser":
            result = start_browser_worker(
                worker_id,
                task,
                carbon_id,
                incognito=incognito,
                resume=True,
            )
        elif worker_type == "terminal":
            result = start_terminal_worker(
                worker_id,
                task,
                carbon_id,
                resume=True,
            )
        elif worker_type == "writer":
            result = start_writer_worker(
                worker_id,
                task,
                carbon_id,
                resume=True,
            )
        else:
            result = f"Error: Worker '{worker_id}' has invalid worker_type '{worker_type}'."
    finally:
        scope.__exit__(None, None, None)
    if result.startswith("Error:") and release_activity is not None:
        release_activity(activity)
    return result


# --- Public API: querying, stopping, listing ---

def _is_process_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_worker_status(worker_id, carbon_id):
    worker_record = _get_worker_record(worker_id)
    if not worker_record:
        return f"Error: Worker '{worker_id}' not found."
    if worker_record.get("carbon_id") != carbon_id:
        return f"Error: Worker '{worker_id}' does not belong to you."

    queue = _load_browser_queue()
    for i, q in enumerate(queue):
        if q["worker_id"] == worker_id:
            return f"Worker '{worker_id}' status: queued (position {i+1} in browser queue)"

    active = _load_active()
    if worker_id not in active:
        archive_id = worker_record.get("last_archive_id", "")
        if archive_id:
            provider = worker_record.get("provider", "unknown")
            return f"Worker '{worker_id}' is idle ({provider}). Last archived run: {archive_id}"
        return f"Worker '{worker_id}' is idle. No archived runs yet."

    worker_info = active[worker_id]
    if _sync_session_id(worker_id, worker_info):
        worker_info = _load_active().get(worker_id, worker_info)

    output_path = worker_info.get("output_path")
    if not output_path or not os.path.exists(output_path):
        return f"Worker '{worker_id}' is active, but its output file is missing."

    with open(output_path) as f:
        raw = f.read()

    parsed = _parse_worker_output(raw, worker_info.get("provider", "claude"))
    worker_type = worker_info.get("worker_type", "unknown")
    provider = worker_info.get("provider", "unknown")
    status = "running" if _is_process_running(worker_info.get("pid")) else "completed"
    return f"Worker '{worker_id}' ({worker_type}, {provider}) status: {status}\n\nOutput so far:\n{parsed}"


def stop_worker(worker_id, carbon_id):
    found_in_queue = False
    queued_activity = {}
    with file_lock(BROWSER_QUEUE_FILE):
        queue = _load_browser_queue()
        for q in queue:
            if q["worker_id"] == worker_id:
                if q.get("carbon_id") != carbon_id:
                    return f"Error: Worker '{worker_id}' does not belong to you."
                found_in_queue = True
                queued_activity = q.get("maintenance_activity") or {}
                break
        if found_in_queue:
            _save_browser_queue(
                [q for q in queue if q["worker_id"] != worker_id]
            )

    if found_in_queue:
        _release_maintenance_activity(queued_activity)
        try:
            from interface.cron.checkback import remove_checkback
            remove_checkback(worker_id)
        except Exception:
            pass
        try:
            from interface.work_updates import record_worker_state

            record_worker_state(
                carbon_id,
                worker_id,
                "cancelled",
                "Worker was removed from the queue",
            )
        except Exception:
            pass
        return f"Done. Worker '{worker_id}' removed from browser queue."

    with file_lock(ACTIVE_FILE):
        active = _load_active()
        if worker_id not in active:
            return f"Error: Worker '{worker_id}' is not active."
        if active[worker_id].get("carbon_id") != carbon_id:
            return f"Error: Worker '{worker_id}' does not belong to you."

        worker_info = active[worker_id]
        if _sync_session_id(worker_id, worker_info):
            worker_info = _load_active().get(worker_id, worker_info)

    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(worker_info["pid"])], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(worker_info["pid"]), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass

    _cleanup_silicon_browser_session(worker_id, worker_info)

    _remove_active_worker(worker_id, worker_info.get("run_id", ""))
    _release_maintenance_activity(worker_info.get("maintenance_activity") or {})

    try:
        from interface.cron.checkback import remove_checkback
        remove_checkback(worker_id)
    except Exception:
        pass
    try:
        from interface.work_updates import record_worker_state

        record_worker_state(
            carbon_id,
            worker_id,
            "cancelled",
            "Worker was stopped",
        )
    except Exception:
        pass

    archive_id = _archive_active_output(worker_id, worker_info, carbon_id)
    queue_result, queue_carbon_id = _process_browser_queue()
    if archive_id:
        _update_worker_record(worker_id, last_archive_id=archive_id, last_used_at=time.time())
        suffix = f" {queue_result}" if queue_result and queue_carbon_id == carbon_id else ""
        return f"Done. Worker '{worker_id}' stopped. Output archived as '{archive_id}'{suffix}"

    suffix = f" {queue_result}" if queue_result and queue_carbon_id == carbon_id else ""
    return f"Done. Worker '{worker_id}' stopped.{suffix}"


def list_active(carbon_id):
    active = _load_active()
    queue = _load_browser_queue()

    my_active = {wid: info for wid, info in active.items() if info.get("carbon_id") == carbon_id}
    my_queue = [q for q in queue if q.get("carbon_id") == carbon_id]

    if not my_active and not my_queue:
        return "No active or queued workers."

    lines = []
    if my_active:
        for wid, info in my_active.items():
            elapsed = time.time() - info["started"]
            minutes = int(elapsed // 60)
            wtype = info.get("worker_type", "unknown")
            provider = info.get("provider", "unknown")
            lines.append(f"- {wid} ({wtype}, {provider}, pid: {info['pid']}, running for {minutes}m, task: {info['task'][:80]})")

    if my_queue:
        lines.append("")
        lines.append("Browser queue (your position in global queue):")
        for i, q in enumerate(queue):
            if q.get("carbon_id") == carbon_id:
                lines.append(f"  position {i+1}. {q['worker_id']} (task: {q['task'][:80]})")

    return "Active workers:\n" + "\n".join(lines)


def list_archive(carbon_id):
    if not os.path.exists(OUTPUTS_DIR):
        return "No archives."

    meta = _load_archive_meta()
    archives = []
    for archive_id, info in meta.items():
        if info.get("carbon_id") == carbon_id:
            fpath = os.path.join(OUTPUTS_DIR, f"{archive_id}.log")
            if os.path.exists(fpath):
                provider = info.get("provider", "unknown")
                archives.append(f"- {archive_id} ({provider})")

    if not archives:
        return "No archives."

    return "Your archived workers:\n" + "\n".join(sorted(archives))


def read_archive(archive_id, carbon_id):
    meta = _load_archive_meta()
    archive_info = meta.get(archive_id)

    if archive_info and archive_info.get("carbon_id") != carbon_id:
        return f"Error: Archive '{archive_id}' does not belong to you."

    archive_path = os.path.join(OUTPUTS_DIR, f"{archive_id}.log")
    if not os.path.exists(archive_path):
        return f"Error: Archive '{archive_id}' not found."

    with open(archive_path) as f:
        raw = f.read()

    provider = archive_info.get("provider", "claude") if archive_info else "claude"
    return _parse_worker_output(raw, provider)


# --- Event loop handlers ---

def _has_completion_event(output_path, provider):
    if not output_path or not os.path.exists(output_path):
        return False
    raw = _read_text_file(output_path)
    return _worker_provider(provider).has_completion_event(raw)


def _worker_terminal_state(raw, provider):
    """Return the real terminal state without publishing raw worker output."""
    return _worker_provider(provider).terminal_state(raw)


def _worker_completion_context(completion):
    return (
        f"Worker '{completion['worker_id']}' "
        f"({completion['worker_type']}, {completion['provider']}) completed:\n"
        f"Result: {completion['result']}\n"
        "Complete worker output archived with id: "
        f"{completion['archive_id']}"
    )


_sweep_call_counter = 0
_SWEEP_INTERVAL = 10


def _record_worker_diagnostics(worker_id, worker_info, carbon_id, provider, output_path):
    """Create a child run linked back to every message that spawned the worker."""
    try:
        from diagnostics.store import Diagnostics
        from interface.progress import DONE, usage_from_done_event
        parent_run_id = worker_info.get("diag_parent_run_id")
        if not parent_run_id:
            return
        child_trace = Diagnostics.start_run(
            trigger="worker",
            carbon_id=carbon_id,
            parent_run_id=parent_run_id,
            room_id=worker_info.get("diag_room_id", ""),
            message_ids=worker_info.get("diag_message_ids") or [],
            meta={"worker_id": worker_id},
        )
        raw = _read_text_file(output_path)
        events = json_events(raw)
        state = {}
        done_events = []
        with child_trace.span("worker.execution") as worker_span:
            worker_span.set_meta(
                worker_id=worker_id,
                worker_type=worker_info.get("worker_type", "unknown"),
                provider=provider,
                worker_run_id=worker_info.get("run_id", ""),
                started_at_ms=int(float(worker_info.get("started") or time.time()) * 1000),
            )
            with child_trace.span("provider_call") as provider_span:
                provider_span.set_meta(provider=provider, worker_id=worker_id)
                sequence = 0
                engine = _worker_provider(provider)
                for event in events:
                    for item in engine.progress_events(event, state):
                        sequence += 1
                        child_trace.event(
                            "worker.progress",
                            sequence=sequence,
                            kind=str(item.get("kind") or ""),
                            status=str(item.get("status") or ""),
                            item_id=str(item.get("item_id") or ""),
                            tool_name=str(item.get("tool_name") or ""),
                            path=str(item.get("path") or "")[:500],
                            query=str(item.get("query") or "")[:500],
                            command=str(item.get("command") or "")[:500],
                        )
                        if item.get("kind") == DONE:
                            done_events.append(item)
                for item in done_events:
                    provider_span.set_tokens(**usage_from_done_event(item))
        child_trace.close()
    except Exception:
        pass


def check_completed_workers():
    global _sweep_call_counter
    _sweep_call_counter += 1
    if _sweep_call_counter >= _SWEEP_INTERVAL:
        _sweep_call_counter = 0
        sweep_orphaned_daemons()

    # Worker subprocesses cannot update the coordinator themselves. The
    # runtime sweep heartbeats both active and profiled-browser queued work so
    # a long accepted task remains a hard blocker for maintenance.
    for queued in _load_browser_queue():
        if isinstance(queued, dict):
            _heartbeat_maintenance_activity(
                queued.get("maintenance_activity") or {}
            )

    active = _load_active()
    for worker_info in active.values():
        if isinstance(worker_info, dict):
            _heartbeat_maintenance_activity(
                worker_info.get("maintenance_activity") or {}
            )
    completed_by_carbon = {}

    for worker_id, initial_info in list(active.items()):
        worker_info = initial_info
        provider = worker_info.get("provider", "claude")
        output_path = worker_info.get("output_path")

        if _sync_session_id(worker_id, worker_info):
            worker_info = _load_active().get(worker_id, worker_info)
            output_path = worker_info.get("output_path")

        process_running = _is_process_running(worker_info.get("pid"))
        if process_running and not _has_completion_event(output_path, provider):
            continue

        worker_info = _claim_completed_worker(
            worker_id,
            worker_info.get("run_id", ""),
        )
        if not worker_info:
            continue

        worker_type = worker_info.get("worker_type", "unknown")
        carbon_id = worker_info.get("carbon_id", "unknown")
        _record_worker_diagnostics(worker_id, worker_info, carbon_id, provider, output_path)

        _cleanup_silicon_browser_session(worker_id, worker_info)

        raw = _read_text_file(output_path)
        result_text = _parse_worker_output(raw, provider)
        terminal_state = _worker_terminal_state(raw, provider)
        try:
            from interface.work_updates import record_worker_state

            record_worker_state(
                carbon_id,
                worker_id,
                terminal_state,
                (
                    "Worker completed"
                    if terminal_state == "completed"
                    else "Worker execution failed"
                ),
            )
        except Exception:
            pass
        archive_id = _archive_active_output(worker_id, worker_info, carbon_id)
        if archive_id:
            _update_worker_record(worker_id, last_archive_id=archive_id, last_used_at=time.time())

        completion = {
            "worker_id": worker_id,
            "worker_type": worker_type,
            "provider": provider,
            "carbon_id": carbon_id,
            "archive_id": archive_id,
            "result": result_text,
        }
        activity_reference = worker_info.get("maintenance_activity") or {}
        continuation_queued = False
        if activity_reference:
            try:
                from manager.runtime.maintenance import COORDINATOR

                continuation_queued = COORDINATOR.enqueue_continuation(
                    carbon_id,
                    _worker_completion_context(completion),
                    activity_reference,
                )
            except Exception:
                continuation_queued = False

        _remove_active_worker(
            worker_id,
            worker_info.get("run_id", ""),
            worker_info.get("_completion_claim_token", ""),
        )
        if not continuation_queued:
            _release_maintenance_activity(activity_reference)

        try:
            from interface.cron.checkback import remove_checkback
            remove_checkback(worker_id)
        except Exception:
            pass

        completion["maintenance_continuation_queued"] = continuation_queued
        completed_by_carbon.setdefault(carbon_id, []).append(completion)

    queue_result, queue_carbon_id = _process_browser_queue()
    if queue_result and queue_carbon_id:
        completed_by_carbon.setdefault(queue_carbon_id, []).append({
            "worker_id": "[queue]",
            "worker_type": "browser",
            "provider": "",
            "carbon_id": queue_carbon_id,
            "archive_id": "",
            "result": queue_result,
        })

    return completed_by_carbon


def check_completed_workers_formatted():
    completed_by_carbon = check_completed_workers()
    if not completed_by_carbon:
        return {}

    result = {}
    for carbon_id, completed in completed_by_carbon.items():
        parts = []
        for c in completed:
            if c["worker_id"] == "[queue]":
                parts.append(c["result"])
            elif c.get("maintenance_continuation_queued"):
                # The durable maintenance queue now owns this accepted
                # continuation, including during a drain.
                continue
            else:
                parts.append(_worker_completion_context(c))
        if parts:
            result[carbon_id] = "\n\n".join(parts)
    return result


# --- Output parsing ---

def _parse_worker_output(raw, provider="claude"):
    """The provider's own reading of what a worker produced."""
    return _worker_provider(provider).parse_output(raw)
