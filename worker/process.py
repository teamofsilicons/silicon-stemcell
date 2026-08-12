"""Turning a task into a running, detached worker process.

The provider builds the command; this builds everything around it — the
environment carrying the worker's own `iwantto` identity, the detached process
group, the log file it writes into, and the stdin feeder that keeps it
reachable while it works.

A worker is a subprocess in the middle of a job: there is no way to push a line
into one once it has started. Streaming stdin is that way, and the feeder is
what closes it once the worker reports a result, which is what lets it exit.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid

from helpers.state import update_json
from inference import STDIN_STREAM, STDIN_TASK, WorkerLaunchSpec, get_provider
from prompts.loader import get_worker_prompt
from worker import constants
from worker.constants import (
    BROWSER_WORKER_MODEL,
    IS_WINDOWS,
    SILICON_CONFIG_FILE,
    VALID_WORKER_PROVIDERS,
    WORKER_PROVIDER_FALLBACKS,
    WORKSPACE_ROOT,
)
from worker.registry import (
    _get_worker_record,
    _load_active,
    _record_active_run,
    _run_output_path,
    _update_worker_record,
    _utc_timestamp_slug,
)


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
    from diagnostics import journal
    from iwantto.actor import WORKER, issue_run_env

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
    from diagnostics import journal
    from iwantto import mailbox
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

    update_json(constants.ACTIVE_FILE, {}, remember_session)

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
        env["SILICON_BROWSER_SESSION"] = constants.SILICON_BROWSER_PROFILE
        env["SILICON_BROWSER_PROFILE"] = constants.SILICON_BROWSER_PROFILE
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
    os.makedirs(constants.OUTPUTS_DIR, exist_ok=True)

    command = engine.worker_command(WorkerLaunchSpec(
        worker_id=worker_id,
        worker_type=worker_type,
        task=task,
        system_prompt=system_prompt,
        session_id=session_id,
        resume=resume,
        streaming=_worker_streaming_enabled(),
        cwd=WORKSPACE_ROOT,
        scratch_dir=constants.WORKER_STATE_DIR,
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


