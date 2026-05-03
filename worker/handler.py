import json
import os
import platform
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime, timezone

from prompts.DNA import get_worker_prompt

IS_WINDOWS = platform.system() == "Windows"

CLAUDE_CMD = "claude"
CODEX_CMD = "codex"
if IS_WINDOWS:
    _claude_path = shutil.which("claude") or shutil.which("claude.cmd")
    if _claude_path:
        CLAUDE_CMD = _claude_path
    _codex_path = shutil.which("codex") or shutil.which("codex.cmd")
    if _codex_path:
        CODEX_CMD = _codex_path

WORKER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(WORKER_DIR)
OUTPUTS_DIR = os.path.join(WORKER_DIR, "outputs")
SILICON_CONFIG_FILE = os.path.join(PROJECT_ROOT, "silicon.json")

ACTIVE_FILE = os.path.join(OUTPUTS_DIR, "_active_workers.json")
BROWSER_QUEUE_FILE = os.path.join(OUTPUTS_DIR, "_browser_queue.json")
ARCHIVE_META_FILE = os.path.join(OUTPUTS_DIR, "_archive_meta.json")
WORKER_REGISTRY_FILE = os.path.join(OUTPUTS_DIR, "_worker_registry.json")

BROWSER_WORKER_MODEL = "sonnet"
WORKER_PROVIDER_FALLBACKS = {
    "browser": ["claude"],
    "terminal": ["claude"],
    "writer": ["claude"],
}
VALID_WORKER_PROVIDERS = {"claude", "codex", "chatgpt"}

try:
    from env import BROWSER_PROFILE as _BROWSER_PROFILE
except ImportError:
    _BROWSER_PROFILE = "silicon"
SILICON_BROWSER_PROFILE = _BROWSER_PROFILE


# --- State persistence ---

def _load_active():
    if os.path.exists(ACTIVE_FILE):
        try:
            with open(ACTIVE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save_active(active):
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(ACTIVE_FILE, "w") as f:
        json.dump(active, f, indent=2)


def _load_browser_queue():
    if os.path.exists(BROWSER_QUEUE_FILE):
        try:
            with open(BROWSER_QUEUE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _save_browser_queue(queue):
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(BROWSER_QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)


def _load_archive_meta():
    if os.path.exists(ARCHIVE_META_FILE):
        try:
            with open(ARCHIVE_META_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save_archive_meta(meta):
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(ARCHIVE_META_FILE, "w") as f:
        json.dump(meta, f, indent=2)


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
    if not os.path.exists(WORKER_REGISTRY_FILE):
        return {}

    try:
        with open(WORKER_REGISTRY_FILE) as f:
            registry = json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {}

    changed = False
    for worker_id, record in registry.items():
        _, record_changed = _migrate_worker_record(worker_id, record)
        changed = changed or record_changed

    if changed:
        _save_worker_registry(registry)

    return registry


def _save_worker_registry(registry):
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(WORKER_REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=2)


def _utc_timestamp_slug():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _run_output_path(worker_id, run_id):
    return os.path.join(OUTPUTS_DIR, f"{worker_id}-{run_id}.txt")


def _make_archive_id(worker_id, run_id):
    return f"{worker_id}-{run_id}"


def _get_worker_record(worker_id):
    return _load_worker_registry().get(worker_id)


def _create_worker_record(worker_id, worker_type, carbon_id, incognito=False):
    registry = _load_worker_registry()
    if worker_id in registry:
        return None, f"Error: Worker '{worker_id}' already exists. Use worker/message to prompt it again."

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
    registry[worker_id] = record
    _save_worker_registry(registry)
    return record, ""


def _update_worker_record(worker_id, **updates):
    registry = _load_worker_registry()
    if worker_id not in registry:
        return
    registry[worker_id].update(updates)
    _save_worker_registry(registry)


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


def _is_codex_provider(provider):
    return _normalize_provider(provider) == "codex"


def _archive_active_output(worker_id, worker_info, carbon_id):
    output_path = worker_info.get("output_path")
    if not output_path or not os.path.exists(output_path):
        return ""

    run_id = worker_info.get("run_id") or _utc_timestamp_slug()
    archive_id = _make_archive_id(worker_id, run_id)
    archive_path = os.path.join(OUTPUTS_DIR, f"{archive_id}.txt")

    if os.path.abspath(output_path) != os.path.abspath(archive_path):
        os.rename(output_path, archive_path)

    meta = _load_archive_meta()
    meta[archive_id] = {
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
    _save_archive_meta(meta)
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


def _get_popen_kwargs(env, output_file):
    popen_kwargs = dict(
        stdout=output_file,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        cwd=PROJECT_ROOT,
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


def _extract_json_events(raw):
    events = []
    if not raw.strip():
        return events
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return events


def _extract_codex_session_id_from_raw(raw):
    for event in _extract_json_events(raw):
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return event["thread_id"]
    return ""


def _sync_codex_session_id(worker_id, worker_info=None):
    if worker_info is None:
        worker_info = _load_active().get(worker_id)
    if not worker_info or not _is_codex_provider(worker_info.get("provider")):
        return ""

    if worker_info.get("session_id"):
        return worker_info["session_id"]

    raw = _read_text_file(worker_info.get("output_path"))
    session_id = _extract_codex_session_id_from_raw(raw)
    if not session_id:
        return ""

    active = _load_active()
    if worker_id in active:
        active[worker_id]["session_id"] = session_id
        _save_active(active)

    _update_worker_record(worker_id, session_id=session_id)
    return session_id


def _wait_for_codex_session_id(worker_id, process, output_path, timeout_seconds=20.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        raw = _read_text_file(output_path)
        session_id = _extract_codex_session_id_from_raw(raw)
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

    return "", "Timed out waiting for Codex session id"


def _record_active_run(worker_id, provider, session_id, process, task, worker_type, carbon_id, output_path, incognito, run_id):
    active = _load_active()
    active[worker_id] = {
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
    }
    _save_active(active)
    _update_worker_record(
        worker_id,
        provider=provider,
        session_id=session_id,
        last_used_at=time.time(),
        last_run_id=run_id,
        incognito=incognito,
    )


def _launch_claude_worker_process(worker_id, task, worker_type, carbon_id, incognito=False, resume=False, session_id=""):
    worker_record = _get_worker_record(worker_id)
    if not worker_record:
        return False, f"Error: Worker '{worker_id}' is not registered."

    if not session_id:
        session_id = worker_record.get("session_id") or str(uuid.uuid4())

    run_id = _utc_timestamp_slug()
    output_path = _run_output_path(worker_id, run_id)
    system_prompt, err = get_worker_prompt(worker_type)
    if err:
        return False, err

    prompt_flag = "--append-system-prompt" if worker_type == "terminal" else "--system-prompt"
    cmd = [
        CLAUDE_CMD, "-p",
        "--resume" if resume else "--session-id", session_id,
        prompt_flag, system_prompt,
        "--dangerously-skip-permissions",
        "--output-format=stream-json",
        "--verbose",
    ]
    if worker_type == "browser":
        cmd.extend(["--model", BROWSER_WORKER_MODEL])
    cmd.append(task)

    env = os.environ.copy()
    if worker_type == "browser":
        env["SILICON_BROWSER_SESSION"] = f"incognito-{worker_id}" if incognito else SILICON_BROWSER_PROFILE

    output_file = open(output_path, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(cmd, **_get_popen_kwargs(env, output_file))
    except Exception as e:
        output_file.close()
        return False, f"Claude launch failed: {e}"
    finally:
        output_file.close()

    _record_active_run(worker_id, "claude", session_id, process, task, worker_type, carbon_id, output_path, incognito, run_id)
    mode = "incognito" if incognito else "profiled"
    return True, f"Done. Worker '{worker_id}' ({worker_type}, {mode}, claude) started (pid: {process.pid}, run: {run_id})"


def _build_codex_worker_prompt(worker_type, task, resume=False):
    if resume:
        return task

    system_prompt, err = get_worker_prompt(worker_type)
    if err:
        return ""

    return (
        f"{system_prompt}\n\n"
        "## Task from Manager\n"
        f"{task}\n"
    )


def _launch_codex_worker_process(worker_id, task, worker_type, carbon_id, incognito=False, resume=False, session_id=""):
    worker_record = _get_worker_record(worker_id)
    if not worker_record:
        return False, f"Error: Worker '{worker_id}' is not registered."

    run_id = _utc_timestamp_slug()
    output_path = _run_output_path(worker_id, run_id)
    prompt = _build_codex_worker_prompt(worker_type, task, resume=resume)
    if not prompt:
        return False, f"Error: Could not build {worker_type} worker prompt."

    if resume:
        if not session_id:
            session_id = worker_record.get("session_id", "")
        if not session_id:
            return False, f"Error: Worker '{worker_id}' has no saved codex session id to resume."
        cmd = [
            CODEX_CMD, "exec", "resume", session_id, "-",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "-s", "danger-full-access",
            "-C", PROJECT_ROOT,
        ]
    else:
        cmd = [
            CODEX_CMD, "exec", "-",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "-s", "danger-full-access",
            "-C", PROJECT_ROOT,
        ]

    env = os.environ.copy()
    if worker_type == "browser":
        env["SILICON_BROWSER_SESSION"] = f"incognito-{worker_id}" if incognito else SILICON_BROWSER_PROFILE

    output_file = open(output_path, "w", encoding="utf-8")
    try:
        popen_kwargs = _get_popen_kwargs(env, output_file)
        popen_kwargs["stdin"] = subprocess.PIPE
        process = subprocess.Popen(cmd, **popen_kwargs)
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except BrokenPipeError:
            pass
    except Exception as e:
        output_file.close()
        return False, f"Codex launch failed: {e}"
    finally:
        output_file.close()

    if not resume:
        captured_session_id, detail = _wait_for_codex_session_id(worker_id, process, output_path)
        if not captured_session_id:
            _terminate_process(process)
            return False, f"Codex launch failed: {detail}"
        session_id = captured_session_id

    _record_active_run(worker_id, "codex", session_id, process, task, worker_type, carbon_id, output_path, incognito, run_id)
    mode = "incognito" if incognito else "profiled"
    mode_part = f", {mode}" if worker_type == "browser" else ""
    return True, f"Done. Worker '{worker_id}' ({worker_type}{mode_part}, codex) started (pid: {process.pid}, run: {run_id})"


def _launch_worker_process(worker_id, task, worker_type, carbon_id, incognito=False, resume=False, provider=None, session_id=""):
    provider = _normalize_provider(provider or "claude")

    if provider == "codex":
        return _launch_codex_worker_process(
            worker_id,
            task,
            worker_type,
            carbon_id,
            incognito=incognito,
            resume=resume,
            session_id=session_id,
        )

    return _launch_claude_worker_process(
        worker_id,
        task,
        worker_type,
        carbon_id,
        incognito=incognito,
        resume=resume,
        session_id=session_id,
    )


def _process_browser_queue():
    if _is_profiled_browser_active():
        return None, None

    queue = _load_browser_queue()
    if not queue:
        return None, None

    next_job = queue.pop(0)
    _save_browser_queue(queue)

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
    return f"[Browser Queue] Dequeued and started: {result}", next_job.get("carbon_id", "unknown")


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

    return False, "Error: Could not start worker. " + " | ".join(errors)


def start_browser_worker(worker_id, task, carbon_id, incognito=False, resume=False):
    active = _load_active()
    if worker_id in active:
        return f"Error: Worker '{worker_id}' is already active."

    worker_record = _get_worker_record(worker_id)
    session_id = worker_record.get("session_id", "") if worker_record else ""
    provider = _normalize_provider(worker_record.get("provider", "")) if worker_record else ""

    if incognito:
        if resume:
            _, result = _launch_worker_process(
                worker_id, task, "browser", carbon_id, incognito=True, resume=True, provider=provider or "claude", session_id=session_id
            )
        else:
            _, result = _launch_with_provider_order(worker_id, task, "browser", carbon_id, incognito=True)
        return result

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
    })
    _save_browser_queue(queue)

    position = len(queue)
    action = "resume" if resume else "start"
    return f"Done. Worker '{worker_id}' (browser) queued at position {position}. Will {action} when current profiled browser worker finishes."


def start_terminal_worker(worker_id, task, carbon_id, resume=False):
    active = _load_active()
    if worker_id in active:
        return f"Error: Worker '{worker_id}' is already active."

    worker_record = _get_worker_record(worker_id)
    if not worker_record:
        return f"Error: Worker '{worker_id}' is not registered."

    if resume:
        provider = _normalize_provider(worker_record.get("provider", "claude"))
        session_id = worker_record.get("session_id", "")
        ok, result = _launch_worker_process(
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

    registry = _load_worker_registry()
    registry.pop(worker_id, None)
    _save_worker_registry(registry)
    return result


def start_writer_worker(worker_id, task, carbon_id, resume=False):
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
    _, err = _create_worker_record(worker_id, worker_type, carbon_id, incognito=incognito)
    if err:
        return err

    if worker_type == "browser":
        result = start_browser_worker(worker_id, task, carbon_id, incognito=incognito, resume=False)
        if result.startswith("Error:"):
            registry = _load_worker_registry()
            registry.pop(worker_id, None)
            _save_worker_registry(registry)
        return result
    if worker_type == "terminal":
        return start_terminal_worker(worker_id, task, carbon_id, resume=False)
    if worker_type == "writer":
        result = start_writer_worker(worker_id, task, carbon_id, resume=False)
        if result.startswith("Error:"):
            registry = _load_worker_registry()
            registry.pop(worker_id, None)
            _save_worker_registry(registry)
        return result

    registry = _load_worker_registry()
    registry.pop(worker_id, None)
    _save_worker_registry(registry)
    return f"Error: invalid worker_type '{worker_type}'. Available types: browser, terminal, writer"


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

    if worker_type == "browser":
        return start_browser_worker(worker_id, task, carbon_id, incognito=incognito, resume=True)
    if worker_type == "terminal":
        return start_terminal_worker(worker_id, task, carbon_id, resume=True)
    if worker_type == "writer":
        return start_writer_worker(worker_id, task, carbon_id, resume=True)
    return f"Error: Worker '{worker_id}' has invalid worker_type '{worker_type}'."


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
    if _is_codex_provider(worker_info.get("provider")):
        _sync_codex_session_id(worker_id, worker_info)
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
    queue = _load_browser_queue()
    found_in_queue = False
    for q in queue:
        if q["worker_id"] == worker_id:
            if q.get("carbon_id") != carbon_id:
                return f"Error: Worker '{worker_id}' does not belong to you."
            found_in_queue = True
            break

    if found_in_queue:
        new_queue = [q for q in queue if q["worker_id"] != worker_id]
        _save_browser_queue(new_queue)
        try:
            from core.cron.checkback import remove_checkback
            remove_checkback(worker_id)
        except Exception:
            pass
        return f"Done. Worker '{worker_id}' removed from browser queue."

    active = _load_active()
    if worker_id not in active:
        return f"Error: Worker '{worker_id}' is not active."
    if active[worker_id].get("carbon_id") != carbon_id:
        return f"Error: Worker '{worker_id}' does not belong to you."

    worker_info = active[worker_id]
    if _is_codex_provider(worker_info.get("provider")):
        _sync_codex_session_id(worker_id, worker_info)
        worker_info = _load_active().get(worker_id, worker_info)

    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(worker_info["pid"])], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(worker_info["pid"]), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass

    _cleanup_silicon_browser_session(worker_id, worker_info)

    del active[worker_id]
    _save_active(active)

    try:
        from core.cron.checkback import remove_checkback
        remove_checkback(worker_id)
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
            fpath = os.path.join(OUTPUTS_DIR, f"{archive_id}.txt")
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

    archive_path = os.path.join(OUTPUTS_DIR, f"{archive_id}.txt")
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
    events = _extract_json_events(raw)
    if _is_codex_provider(provider):
        return any(event.get("type") == "turn.completed" for event in events)
    return any(event.get("type") == "result" for event in events)


_sweep_call_counter = 0
_SWEEP_INTERVAL = 10


def check_completed_workers():
    global _sweep_call_counter
    _sweep_call_counter += 1
    if _sweep_call_counter >= _SWEEP_INTERVAL:
        _sweep_call_counter = 0
        sweep_orphaned_daemons()

    active = _load_active()
    completed_by_carbon = {}

    for worker_id in list(active.keys()):
        worker_info = active[worker_id]
        provider = worker_info.get("provider", "claude")
        output_path = worker_info.get("output_path")

        if _is_codex_provider(provider):
            _sync_codex_session_id(worker_id, worker_info)
            active = _load_active()
            worker_info = active.get(worker_id, worker_info)
            output_path = worker_info.get("output_path")

        process_running = _is_process_running(worker_info.get("pid"))
        if process_running and not _has_completion_event(output_path, provider):
            continue

        worker_type = worker_info.get("worker_type", "unknown")
        carbon_id = worker_info.get("carbon_id", "unknown")

        _cleanup_silicon_browser_session(worker_id, worker_info)

        raw = _read_text_file(output_path)
        result_text = _parse_worker_output(raw, provider)
        archive_id = _archive_active_output(worker_id, worker_info, carbon_id)
        if archive_id:
            _update_worker_record(worker_id, last_archive_id=archive_id, last_used_at=time.time())

        del active[worker_id]

        try:
            from core.cron.checkback import remove_checkback
            remove_checkback(worker_id)
        except Exception:
            pass

        completed_by_carbon.setdefault(carbon_id, []).append({
            "worker_id": worker_id,
            "worker_type": worker_type,
            "provider": provider,
            "carbon_id": carbon_id,
            "archive_id": archive_id,
            "result": result_text,
        })

    if completed_by_carbon:
        _save_active(active)

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
            else:
                parts.append(
                    f"Worker '{c['worker_id']}' ({c['worker_type']}, {c['provider']}) completed:\n"
                    f"Result: {c['result']}\n"
                    f"Complete worker output archived with id: {c['archive_id']}"
                )
        result[carbon_id] = "\n\n".join(parts)
    return result


def clean_old_archives(archive_for_seconds):
    if not os.path.exists(OUTPUTS_DIR):
        return {}

    now = time.time()
    meta = _load_archive_meta()
    meta_changed = False

    for fname in os.listdir(OUTPUTS_DIR):
        if fname.startswith("_") or fname == ".gitkeep":
            continue
        fpath = os.path.join(OUTPUTS_DIR, fname)
        if os.path.isfile(fpath):
            age = now - os.path.getmtime(fpath)
            if age > archive_for_seconds:
                os.remove(fpath)
                base = fname.replace(".txt", "")
                if base in meta:
                    del meta[base]
                    meta_changed = True

    if meta_changed:
        _save_archive_meta(meta)

    return {}


# --- Output parsing ---

def _parse_claude_output(raw):
    if not raw.strip():
        return "No output yet."

    events = _extract_json_events(raw)
    if not events:
        return "No parseable output yet."

    result_event = None
    for event in events:
        if event.get("type") == "result":
            result_event = event

    if result_event and result_event.get("result"):
        return result_event["result"]

    texts = []
    seen = set()
    for event in events:
        if event.get("type") == "assistant" and event.get("message", {}).get("content"):
            for block in event["message"]["content"]:
                if block.get("type") == "text":
                    txt = block.get("text", "").strip()
                    if txt and txt not in seen:
                        seen.add(txt)
                        texts.append(txt)

    if texts:
        return texts[-1]
    return "Worker running, no text output yet."


def _parse_codex_output(raw):
    if not raw.strip():
        return "No output yet."

    events = _extract_json_events(raw)
    if not events:
        return "No parseable output yet."

    texts = []
    for event in events:
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = item.get("text", "").strip()
                if text:
                    texts.append(text)

    if texts:
        return texts[-1]

    if any(event.get("type") == "turn.completed" for event in events):
        return "Worker completed with no text output."

    return "Worker running, no text output yet."


def _parse_worker_output(raw, provider="claude"):
    if _is_codex_provider(provider):
        return _parse_codex_output(raw)
    return _parse_claude_output(raw)
