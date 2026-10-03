#!/usr/bin/env python3
"""Real interpreter + Omni + Caddy; scripted Claude protocol avoids paid model calls.
Run after cargo build: OMNI_DAEMON=/path/omnid SILICON_CADDY=/path/caddy python3 tests/e2e.py
"""
import http.server
import errno
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid


def provider():
    output_lock = threading.Lock()
    def emit(value):
        with output_lock:
            print(json.dumps(value), flush=True)
    if sys.argv[1:3] == ["auth", "status"]:
        emit({"loggedIn": True})
        return
    home = Path(os.environ["SILICON_HOME"])
    address = os.environ["ISI"]
    native = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "probe"
    info = {key: os.environ.get(key) for key in ("SILICON_HOME", "ISI", "SI_URL", "SI_TOKEN", "TZ")}
    info.update(pid=os.getpid(), cwd=os.getcwd(), argv=sys.argv[1:])
    (home / (native + ".provider.json")).write_text(json.dumps(info))
    holding = False
    streaming = threading.Event()
    def stream_updates():
        while True:
            time.sleep(0.025)
            if streaming.is_set():
                emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "still working"}]}})
    threading.Thread(target=stream_updates, daemon=True).start()
    for line in sys.stdin:
        value = json.loads(line)
        if value["type"] == "control_request":
            emit({"type": "control_response", "response": {"request_id": value["request_id"], "subtype": "success", "response": {"rate_limits": {}}}})
            continue
        if value["type"] != "user":
            continue
        content = value["message"]["content"]
        message = content if isinstance(content, str) else "\n".join(item.get("text", "") for item in content)
        with (home / "provider-messages.jsonl").open("a") as file:
            file.write(json.dumps({"isi": address, "message": message, "at": time.time()}) + "\n")
        emit({"type": "system", "subtype": "init", "session_id": native, "model": "scripted"})
        emit({"type": "user", "isReplay": True, "session_id": native, "message": {"content": message}})
        if message == "overflow context":
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "errors": ["Prompt is too long", "context_window_exceeded"]})
            continue
        holding = (holding or message.startswith("hold")) and message != "finish"
        if message == "hold pulse":
            streaming.set()
        elif not holding:
            streaming.clear()
        if not holding:
            emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "completed " + message}]}})
            emit({"type": "result", "subtype": "success", "is_error": False})


class Registry(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps({"pick": {"provider": "claude-code-cli", "model": "scripted"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *_):
        pass


def eventually(check, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        assert time.monotonic() < deadline, "condition timed out"
        time.sleep(0.05)


def main():
    root = Path(__file__).resolve().parent.parent
    binary_dir = Path(os.environ.get("SILICON_TEST_BIN_DIR", root / "target/debug"))
    test_base = root.parent / "silicon-interpreter-testing"
    test_base.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="e2e-", dir=test_base))
    print("E2E workspace:", work, flush=True)
    binaries = work / "bin"
    binaries.mkdir()
    provider_path = binaries / "claude"
    provider_path.write_text("#!" + sys.executable + "\n" + Path(__file__).read_text().split("\n", 1)[1])
    provider_path.chmod(0o755)
    # Omni starts CLIs on the PATH the user's login shell reports, ahead of its own. Answer that
    # probe with this run's own environment so a real `claude` on the developer's shell PATH can
    # never shadow the scripted provider.
    shell = binaries / "shell"
    shell.write_text("#!/bin/sh\nfor last; do :; done\nexec /bin/sh -c \"$last\"\n")
    shell.chmod(0o755)
    home = work / "silicon"
    home.mkdir()
    (home / "dna-interval").write_text("0.5s")
    packages = home / ".silicon/packages"
    (packages / ".honeycomb/dir").mkdir(parents=True)
    (packages / ".honeycomb/dir/config.json").write_text(json.dumps({"telemetry": True}))
    (packages / ".silicon-update-policy-migrated").touch()
    state = work / "interpreter"
    state.mkdir()
    registry = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Registry)
    threading.Thread(target=registry.serve_forever, daemon=True).start()
    env = dict(os.environ, SILICON_INTERPRETER_HOME=str(state), SHELL=str(shell), PATH=str(binaries) + os.pathsep + str(binary_dir) + os.pathsep + os.environ["PATH"], OMNI_REGISTRY=f"http://127.0.0.1:{registry.server_port}/choose.json", SILICON_AUTO_UPDATE="0", SILICON_TELEMETRY="0")
    assert shutil.which(env.get("OMNI_DAEMON", "omnid"), path=env["PATH"]), "set OMNI_DAEMON to the real Omni daemon"
    assert shutil.which(env.get("SILICON_CADDY", "caddy"), path=env["PATH"]), "set SILICON_CADDY to real Caddy"
    honeycomb = binaries / "honeycomb"
    honeycomb.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
packages = pathlib.Path(os.environ["SILICON_HOME"])
root = packages.parent.parent
registry = packages / "mock-registry.json"
records = json.loads(registry.read_text()) if registry.exists() else {}
settings = packages / ".honeycomb/dir/config.json"
if settings.exists() and "auto_update" not in json.loads(settings.read_text()):
    sys.exit(str(settings) + " is invalid JSON; repair it before continuing")
args = sys.argv[1:]
if args == ["installed", "--json"]:
    print(json.dumps(records))
elif args[:2] == ["apps", "get"]:
    assert len(args) == 4 and args[3] == "--json", args
    app_id = args[2]
    assert app_id in records, app_id
    print(json.dumps({"app_id": app_id, "name": "Mock " + app_id,
                      "description": "Honeycomb description for " + app_id + "."}))
elif args[:2] == ["uninstall", "dynamic"]:
    assert args == ["uninstall", "dynamic", "--json"], args
    records.pop("dynamic")
    registry.write_text(json.dumps(records))
    print(json.dumps({"status": "uninstalled"}))
else:
    assert len(args) == 3 and args[0] == "install" and args[2] == "--json", args
    assert "PATH" not in os.environ, "private installs must not collide with global app commands"
    app_id = args[1]
    assert app_id in ["iam", "ting", "progress", "dynamic"], app_id
    with (root / "install-calls").open("a") as log: log.write(app_id + "\\n")
    command = {"iam": "iam", "ting": "ting", "progress": "progress", "dynamic": "dynamic"}[app_id]
    path = pathlib.Path(__file__).with_name("ting-stub") if app_id == "ting" else root / ("iam-stub" if app_id == "iam" else "progress-app")
    if app_id == "dynamic":
        path = pathlib.Path(__file__).with_name("dynamic-stub")
    if app_id == "iam":
        path.write_text("#!/bin/sh\\nprintf '%s\\\\n' '{\\\"slt\\\":\\\"isolated-test-slt\\\",\\\"expires_in\\\":60}'\\n")
        path.chmod(0o755)
    records[app_id] = {"app_id": app_id, "commands": {command: str(path)}}
    registry.write_text(json.dumps(records))
    print(json.dumps({"status": "installed"}))
''')
    honeycomb.chmod(0o755)
    env["SILICON_HONEYCOMB"] = str(honeycomb)
    ting = binaries / "ting-stub"
    ting.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
home = pathlib.Path(os.environ["SILICON_HOME"])
args = sys.argv[1:]
if args == ["iam", "--json"]:
    result = {"app_id": "ting"}
elif args in (["login", "status", "--json"], ["auth", "status", "--json"]):
    result = {"authenticated": True}
elif args == ["login", "isolated-test-slt"]:
    assert os.environ["SILICON_ORG"] == json.loads((home / ".silicon/org.json").read_text())
    result = {"authenticated": True}
else:
    with (home / "ting-calls.jsonl").open("a") as log:
        log.write(json.dumps(args) + "\\n")
    saved = home / "ting-hook.json"
    hook = json.loads(saved.read_text()) if saved.exists() else None
    if args[:2] == ["webhook", "list"]:
        result = {"items": [hook] if hook else []}
    elif args[0] == "webhook":
        assert "--json" in args, args
        if "--id" in args:
            assert hook and args[args.index("--id") + 1] == hook["id"], args
        else:
            assert hook is None, "reconnect must reattach the saved hook"
        result = {"id": "hook_e2e", "url": args[1], "state": "connected"}
        saved.write_text(json.dumps(result))
    elif args[0] == "unhook":
        assert args == ["unhook", hook["id"], "--json"], args
        hook["state"] = "detached"
        saved.write_text(json.dumps(hook))
        result = {"id": hook["id"], "removed": True}
    elif args == ["list", "--json"]:
        result = {"items": [hook] if hook else []}
    else:
        sys.exit("unexpected Ting arguments: " + repr(args))
print(json.dumps(result))
''')
    ting.chmod(0o755)
    dynamic = binaries / "dynamic-stub"
    dynamic.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
home = pathlib.Path(os.environ["SILICON_HOME"])
assert os.environ["SILICON_ORG"] == "local"
args = sys.argv[1:]
token = home / "dynamic-authenticated"
if args == ["iam", "--json"]:
    result = {"app_id": "dynamic"}
elif args == ["login", "status", "--json"]:
    result = {"authenticated": token.exists()}
elif args == ["login", "isolated-test-slt"]:
    token.touch()
    result = {"authenticated": True}
elif args == ["logout", "--help"]:
    result = {}
elif args == ["logout"]:
    token.unlink()
    result = {"authenticated": False}
elif args[:2] == ["config", "set"]:
    assert len(args) == 3, args
    result = json.loads(args[2])
    (home / "dynamic-config.json").write_text(json.dumps(result))
else:
    sys.exit("unexpected dynamic app arguments: " + repr(args))
print(json.dumps(result))
''')
    dynamic.chmod(0o755)
    config = home / "silicon.yaml"
    config.write_text(f'''silicon:
  id: si:e2e
  org_id: local
  token: isolated-e2e-token
  timezone: Asia/Kolkata
  SILICON_HOME: {json.dumps(str(home))}
  inference_providers: [claude-code-cli]
  setup:
    - '! printf "setup-out\\n"; printf "setup-err\\n" >&2; printf x >> setup-count'
isi:
  source:
    model: fast
    primary_send_mode: global
    session_type: persistent
    dna:
      assemble: ['! printf "home=%s isi=%s" "$SILICON_HOME" "$ISI"']
      next_refresh: 30min
  target:
    model: fast
    primary_send_mode: session
    session_type: persistent
    dna: {{assemble: [], next_refresh: 30min}}
  ephemeral:
    model: fast
    primary_send_mode: global
    session_type: ephemeral
    dna: {{assemble: [], next_refresh: 30min}}
  ephemeral_session:
    model: fast
    primary_send_mode: session
    session_type: ephemeral
    dna: {{assemble: [], next_refresh: 30min}}
  restricted:
    model: fast
    primary_send_mode: global
    session_type: persistent
    dna: {{assemble: [], next_refresh: 30min}}
  pulse:
    model: fast
    primary_send_mode: session
    session_type: persistent
    dna:
      assemble:
        - '! printf "%s\\n" "$ISI" >> "$SILICON_HOME/dna-assemblies"; if test -f "$SILICON_HOME/slow-refresh"; then touch "$SILICON_HOME/refresh-started"; while ! test -f "$SILICON_HOME/refresh-release"; do sleep 0.05; done; touch "$SILICON_HOME/refresh-finished"; fi; printf "pulse DNA for %s" "$ISI"'
      next_refresh: '! printf "%s\\n" "$ISI" >> "$SILICON_HOME/dna-deadlines"; cat "$SILICON_HOME/dna-interval"'
    heartbeat:
      next: '! printf "%s\\n" "$ISI" >> "$SILICON_HOME/heartbeat-deadlines"; printf 0.5s'
      message: 'true'
    new_session_suggestion:
      min_new_messages: 2
      cooldown_minutes: 3s
      suggestion_message: '! printf "suggest %s" "$ISI"'
access:
  source: [target, ephemeral, ephemeral_session, pulse]
  target: [source]
  ephemeral: [source]
  ephemeral_session: [source]
  restricted: []
  pulse: [source]
flow:
  - if:
      condition: '{{request.tings[0].type == "gated"}}'
      then:
        - log:
            message: '! touch "$SILICON_HOME/flow-started"; while ! test -f "$SILICON_HOME/flow-release"; do sleep 0.05; done; printf gate-released'
  - var:
      name: payload
      value: '{{request.tings[0].data}}'
  - send:
      isi: source
      message: '{{var.payload.message}}'
  - log:
      message: 'flow finished {{request.tings[0].type}}'
  - if:
      condition: '{{request.tings[0].type == "batch"}}'
      then:
        - send:
            isi: source
            message: '{{request.tings[1].data.message}}'
  - if:
      condition: '{{request.tings[0].type == "new_message"}}'
      then:
        - var:
            name: new_message
            value: '{{make_readable(request.tings[0])}}'
        - send:
            isi: source
            message: '{{var.new_message}}'
  - if:
      condition: '{{request.tings[0].type == "ephemeral"}}'
      then:
        - send:
            isi: ephemeral_session
            session_id: '{{request.tings[0].data.session_id}}'
            message: '{{request.tings[0].data.message}}'
''')
    original = config.read_bytes()
    binary = binary_dir / "silicon"
    def cli(*args, ok=True, child_env=env, cwd=None):
        out = subprocess.run([str(binary), *args], env=child_env, cwd=cwd, capture_output=True, text=True, timeout=35)
        assert (out.returncode == 0) == ok, out.stdout + out.stderr
        return out.stdout
    occupied = socket.socket()
    try:
        occupied.bind(("127.0.0.1", 1823))
        occupied.listen()
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
    log = (work / "server.log").open("w")
    process = subprocess.Popen([str(binary), "serve", "--port", "1823"], env=env, cwd=home, stdout=log, stderr=log)
    try:
        def started():
            assert process.poll() is None, (work / "server.log").read_text()
            path = state / "daemon.json"
            return json.loads(path.read_text()) if path.exists() else None
        daemon = eventually(started)
        assert daemon["port"] == 1822, daemon
        base = f'http://127.0.0.1:{daemon["port"]}'
        def post(path, value, token=None, host=None, status=200):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            if host:
                headers["Host"] = host
            request = urllib.request.Request(base + path, data=json.dumps(value).encode(), headers=headers)
            try:
                response = urllib.request.urlopen(request, timeout=35)
            except urllib.error.HTTPError as error:
                response = error
            body = response.read()
            try:
                result = json.loads(body) if body else None
            except ValueError as error:
                raise AssertionError((response.status, error, body.decode(errors="replace"))) from error
            assert response.status == status, (response.status, result)
            return result
        def control(action, **args):
            return post("/control", {"action": action, "args": args}, daemon["token"])
        def event(value, host="e2e.local.localhost", status=204):
            value = {"id": str(uuid.uuid4()), **value}
            return post("/events", {"tings": [value]}, host=host, status=status)
        def source_event(text, kind="injected", contains=False):
            if not control("sessions", silicon="si:e2e", isi="source"):
                return None
            progress = control("show", silicon="si:e2e", isi="source")
            return next((row for row in progress["events"] if row["type"] == kind
                         and (text in row.get("text", "") if contains else row.get("text") == text)), None)
        # A gated app status check proves connect reports authentication before it finishes.
        progress_home = work / "progress"
        progress_home.mkdir()
        # Start from the package home 4.0.6 left behind: migrated, without Honeycomb's
        # required auto_update setting, which made every command in that home fail.
        progress_packages = progress_home / ".silicon/packages"
        (progress_packages / ".honeycomb/dir").mkdir(parents=True)
        (progress_packages / ".honeycomb/dir/config.json").write_text(json.dumps({"telemetry": True}))
        (progress_packages / ".silicon-update-policy-migrated").touch()
        progress_config = progress_home / "silicon.yaml"
        progress_config.write_text(original.decode().replace("id: si:e2e", "id: si:progress")
            .replace(json.dumps(str(home)), json.dumps(str(progress_home)))
            .replace("  source:\n", "  source:\n    apps: ['progress']\n")
            .replace("  target:\n", "  target:\n    apps: ['progress']\n"))
        progress_app = progress_home / "progress-app"
        progress_app.write_text('''#!/bin/sh
case "$*" in
  'iam --json') echo '{"app_id":"progress"}' ;;
  'auth token isolated-test-slt') echo '{"authenticated":true}' ;;
  'auth status --json')
    touch auth-started
    while ! test -f auth-release; do sleep 0.05; done
    if test -f auth-fail; then echo 'progress: session store unreachable' >&2; echo '{"authenticated":null}'; exit 1; fi
    echo '{"authenticated":true}' ;;
  *) exit 1 ;;
esac
''')
        progress_app.chmod(0o755)
        connecting = subprocess.Popen([str(binary), "connect", str(progress_config)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(iter(connecting.stdout.readline, "")), daemon=True)
        reader.start()
        try:
            eventually(lambda: any("… Authenticating progress" in line for line in lines))
            assert connecting.poll() is None, "progress was buffered until connect returned"
            assert not any("✓ Authenticated progress" in line for line in lines)
            (progress_home / "auth-release").touch()
            connecting.wait(timeout=35)
            reader.join(timeout=5)
            assert connecting.returncode == 0, connecting.stderr.read()
            assert any("✓ Authenticated progress" in line for line in lines), lines
            assert not any("\x1b" in line for line in lines), "redirected output must not contain terminal control codes"
        finally:
            (progress_home / "auth-release").touch()
            if connecting.poll() is None:
                connecting.terminate()
                connecting.wait(timeout=15)
        assert (progress_home / "install-calls").read_text().splitlines() == ["iam", "progress", "ting"]
        assert json.loads((progress_home / "ting-hook.json").read_text())["url"] == "http://progress.local.localhost/events"
        assert json.loads((progress_home / ".silicon/packages/.honeycomb/dir/config.json").read_text()) == {"auto_update": True, "telemetry": True}, "a package home left without Honeycomb's required setting must be repaired"
        cli("disconnect", "si:progress")
        (progress_home / "auth-fail").touch()
        (progress_home / "auth-started").unlink()
        result = json.loads(cli("--json", "connect", str(progress_config)))
        assert result["connection"]["id"] == "si:progress"
        assert not (progress_home / "auth-started").exists(), "reconnect must reuse the cached auth check"
        assert (progress_home / "install-calls").read_text().splitlines() == ["iam", "progress", "ting"] * 2, "every connect must install without a version even while auth is cached"
        hook_calls = [json.loads(line) for line in (progress_home / "ting-calls.jsonl").read_text().splitlines()]
        attaches = [args for args in hook_calls if args[:2] == ["webhook", "http://progress.local.localhost/events"]]
        assert len(attaches) == 2 and "--id" not in attaches[0], hook_calls
        assert attaches[1][attaches[1].index("--id") + 1] == "hook_e2e", hook_calls
        assert ["unhook", "hook_e2e", "--json"] in hook_calls
        cli("disconnect", "si:progress")
        checked = progress_home / ".silicon/auth-checked.json"
        checked.write_text(json.dumps({key: int(time.time()) - 48 * 60 * 60 for key in json.loads(checked.read_text())}))
        failed = subprocess.run([str(binary), "connect", str(progress_config)], env=env, capture_output=True, text=True, timeout=35)
        # A failed automatic check no longer keeps the Silicon offline: it connects, and the
        # check is shown failing live with the app's own words.
        assert failed.returncode == 0 and "✗ Authenticating progress" in failed.stdout, failed.stdout + failed.stderr
        assert "✓ Authenticated progress" not in failed.stdout
        # The app's own words reach the Carbon whole: command, exit status and both streams.
        for said in [" auth status --json` failed: exit status: 1", "progress: session store unreachable", '{"authenticated":null}']:
            assert said in failed.stdout, (said, failed.stdout + failed.stderr)
        assert "automatic credential check failed" in (progress_home / ".silicon/silicon.log").read_text()
        cli("disconnect", "si:progress")
        cli("compile", str(config))
        assert not (home / "setup-count").exists(), "compile must not run setup"
        result = cli("connect", str(config))
        assert "setup-out" in result and "setup-err" in result, result
        assert (home / "setup-count").read_text() == "x", "setup must run once per connection"
        assert json.loads(cli("ping", "si:e2e"))["online"]
        assert not json.loads(cli("ping", "si:missing"))["online"]
        assert "isolated-e2e-token" not in cli("config", "si:e2e")
        assert json.loads(cli("settings", "set", "telemetry", "--off"))["telemetry"] is False
        assert control("settings")["auto_update"] is True
        assert json.loads(cli("settings", "get", "telemetry")) is False
        cli("settings", "set", "unknown", "--off", ok=False)
        report = json.loads(cli("bug-report", "--title", "test", "--body", "repro", "--pr", "https://github.com/teamofsilicons/silicon-stemcell/pull/1", "--dry-run"))
        assert "Fix PR:" in report["body"]
        request = urllib.request.Request(base + "/ping", headers={"Host":"e2e.local.localhost"})
        assert json.loads(urllib.request.urlopen(request).read())["online"]
        assert "si:e2e" in cli("ls", "si:*")
        assert len(control("list")) == 1
        other_home = work / "other home"
        other_home.mkdir()
        other_config = other_home / "silicon.yaml"
        other_config.write_text(original.decode().replace("id: si:e2e", "id: si:path").replace(json.dumps(str(home)), json.dumps(str(other_home))))
        cli("connect", "./silicon.yaml", cwd=other_home)
        assert {row["id"] for row in control("list")} == {"si:e2e", "si:path"}
        missing_home = work / "missing"
        missing_home.mkdir()
        cli("disconnect", "./silicon.yaml", cwd=missing_home, ok=False)
        post("/control", {"action": "disconnect", "args": {"target": "./silicon.yaml"}}, daemon["token"], status=400)
        assert {row["id"] for row in control("list")} == {"si:e2e", "si:path"}
        cli("disconnect", "./silicon.yaml", cwd=other_home)
        assert [row["id"] for row in control("list")] == ["si:e2e"]
        cli("connect", "./silicon.yaml", cwd=other_home)
        (other_home / "alias.yaml").symlink_to("silicon.yaml")
        cli("disconnect", "./alias.yaml", cwd=other_home)
        assert [row["id"] for row in control("list")] == ["si:e2e"]
        post("/control", {"action": "list", "args": {}}, status=401)
        # A live generic flow receives each JSON shape without the Ting envelope. Malformed
        # Ting-shaped JSON is ordinary data too, and repeated generic inputs are distinct.
        config.write_text(original.decode().split("\nflow:\n", 1)[0] + "\nflow:\n  - log: {message: 'GENERIC_FLOW_RECEIVED {request}'}\n")
        generic_requests = [
            {"type": "ping", "data": {}, "metadata": {}},
            {"tings": [{"id": "malformed-ting", "type": "ping", "data": [], "metadata": {}}]},
            [1, {"nested": True}], "generic-string", 42, True, None,
            {"type": "ping", "data": {}, "metadata": {}},
        ]
        generic_counts = {}
        try:
            for payload in generic_requests:
                post("/events", payload, host="e2e.local.localhost", status=204)
                rendered = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, separators=(",", ":"))
                marker = "[GENERIC_FLOW_RECEIVED " + rendered + "]"
                generic_counts[marker] = generic_counts.get(marker, 0) + 1
                eventually(lambda: (home / ".silicon/silicon.log").read_text().count(marker) == generic_counts[marker])
        finally:
            config.write_bytes(original)
        event({"type": "ping", "data": {}, "metadata": {}}, host="other.local.localhost", status=404)
        # A Ting acknowledgement means durable receipt; a blocked flow must not hold it open.
        gate_batch = {"tings": [{"id": "gate-event", "type": "gated", "data": {"message": "gated delivery"}, "metadata": {}}]}
        start = time.monotonic()
        try:
            post("/events", gate_batch, host="e2e.local.localhost", status=204)
            assert time.monotonic() - start < 5, "Ting receipt waited for the blocked flow"
            eventually(lambda: (home / "flow-started").exists())
            post("/events", gate_batch, host="e2e.local.localhost", status=204)
            assert not control("sessions", silicon="si:e2e", isi="source"), "flow gate did not block sending"
        finally:
            (home / "flow-release").touch()
        eventually(lambda: source_event("gated delivery", "start"))
        eventually(lambda: control("sessions", silicon="si:e2e", isi="source")[0]["status"] == "idle")
        event({"type": "ping", "data": {"message": "hold first"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: source_event("hold first", "start"))
        record = control("sessions", silicon="si:e2e", isi="source")[0]
        assert record["status"] == "running"
        event({"type": "ping", "data": {"message": "mid-turn"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: source_event("mid-turn"))
        progress = control("show", silicon="si:e2e", isi="source")
        assert any(event["type"] == "injected" and event["text"] == "mid-turn" for event in progress["events"]), progress
        assert not any(row["type"] == "end" and row["turn"] == source_event("hold first", "start")["turn"] for row in progress["events"])
        event({"type": "new_message", "data": {"message": "hola", "sender": {"id": "c:shubham", "type": "carbon"}, "reply_to": None}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: source_event("message: hola", contains=True))
        progress = control("show", silicon="si:e2e", isi="source")
        assert any(event["type"] == "injected" and "message: hola" in event["text"] and "  sender:\n    id: c:shubham\n    type: carbon" in event["text"] for event in progress["events"]), progress
        provider_info = eventually(lambda: next((json.loads(file.read_text()) for file in home.glob("*.provider.json") if json.loads(file.read_text())["ISI"] == "source"), None))
        assert provider_info["SILICON_HOME"] == str(home)
        assert provider_info["cwd"] == str(home)
        assert provider_info["TZ"] == "Asia/Kolkata"
        prompt = provider_info["argv"][provider_info["argv"].index("--system-prompt") + 1]
        assert "isi=source" in prompt and "target (session)" in prompt and "restricted" not in prompt
        assert ".silicon/apps.md\nApp Name: Mock ting\nApp Id: ting" in prompt
        assert "CLI: run `ting --help` to know about it\n\nAbout: Honeycomb description for ting." in prompt
        child_env = dict(env, **{key: provider_info[key] for key in ("SILICON_HOME", "ISI", "SI_URL", "SI_TOKEN", "TZ")})
        def si(*args, ok=True, context=child_env):
            result = subprocess.run([str(binary_dir / "si"), *args], env=context, capture_output=True, text=True, timeout=35)
            assert (result.returncode == 0) == ok, result.stdout + result.stderr
            return result.stdout
        si("app", "uninstall", "ting", ok=False)
        si("app", "install", "dynamic")
        assert "dynamic" in config.read_text() and (home / ".silicon/bin/dynamic").is_symlink()
        app_config = control("configuration", silicon="si:e2e")
        assert app_config["isi"]["source"]["apps"] == ["dynamic"]
        assert all("dynamic" not in isi["apps"] for name, isi in app_config["isi"].items() if name != "source")
        assert not app_config["silicon"]["apps"], "new app installs belong to the calling ISI"
        assert "App Name: Mock dynamic\nApp Id: dynamic" in (home / ".silicon/apps.md").read_text()
        assert (home / "dynamic-authenticated").exists()
        config.write_text(config.read_text().replace("silicon:\n", '''silicon:
  app_configs:
    dynamic:
      org: '{silicon.SILICON_ORG}'
      nested: [true, 3, 'quoted space']
''', 1))
        si("app", "install", "dynamic")
        assert json.loads((home / "dynamic-config.json").read_text()) == {"org": "local", "nested": [True, 3, "quoted space"]}
        si("app", "uninstall", "dynamic")
        assert "dynamic" not in config.read_text(), "uninstall must remove both app and configuration"
        assert "App Id: dynamic" not in (home / ".silicon/apps.md").read_text()
        assert not (home / ".silicon/bin/dynamic").exists() and not (home / "dynamic-authenticated").exists()
        cli("compile", str(config))
        original = config.read_bytes()  # Subsequent operations must preserve the deliberately edited YAML.
        si("isi", "send", "restricted", "forbidden", ok=False)
        si("isi", "send", "ephemeral_session", "missing title", "--id", "untitled", "--new", ok=False)
        post("/control", {"action": "send", "args": {"silicon": "si:e2e", "isi": "ephemeral_session", "id": "untitled", "new": True, "message": "missing title"}}, daemon["token"], status=400)
        assert not control("sessions", silicon="si:e2e", isi="ephemeral_session")
        event({"type": "ephemeral", "data": {"session_id": "flow-job", "message": "hold ephemeral"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: any(row["id"] == "flow-job" and row["status"] == "running"
                               for row in control("sessions", silicon="si:e2e", isi="ephemeral_session")))
        ephemeral_session = control("show", silicon="si:e2e", isi="ephemeral_session", id="flow-job")["session"]
        assert ephemeral_session["title"] == "flow-job" and ephemeral_session["status"] == "running"
        si("isi", "send", "ephemeral_session", "follow-up", "--id", "flow-job")
        si("isi", "send", "ephemeral_session", "finish", "--id", "flow-job")
        eventually(lambda: not control("sessions", silicon="si:e2e", isi="ephemeral_session"))
        si("isi", "send", "target", "missing", "--id", "missing", ok=False)
        si("isi", "send", "target", "hold target", "--id", "job", "--new", "--title", "A job")
        assert control("sessions", silicon="si:e2e", isi="target")[0]["id"] == "job"
        si("isi", "send", "target", "finish", "--id", "job")
        eventually(lambda: control("sessions", silicon="si:e2e", isi="target")[0]["status"] == "idle")
        si("isi", "end", "target", "--id", "job")
        assert not control("sessions", silicon="si:e2e", isi="target")
        archive = control("sessions", silicon="si:e2e", isi="target", archived=True)[0]
        assert archive["id"] == "job" and archive["archived_at"]
        si("isi", "send", "target", "history question", "--id", "job", "--archived")
        si("isi", "send", "ephemeral", "short task")
        eventually(lambda: not control("sessions", silicon="si:e2e", isi="ephemeral"))
        eventually(lambda: any(event["type"] == "injected" and "ephemeral completed:" in event["text"] for event in control("show", silicon="si:e2e", isi="source")["events"]))
        event({"type": "ping", "data": {"message": "finish"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: control("sessions", silicon="si:e2e", isi="source")[0]["status"] == "idle")

        def messages(address, text=None):
            path = home / "provider-messages.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            return [row for row in rows if row["isi"] == address and (text is None or row["message"] == text)]
        def session(isi, session_id=None):
            return next(row for row in control("sessions", silicon="si:e2e", isi=isi) if session_id is None or row["id"] == session_id)
        def lines(name):
            path = home / name
            return path.read_text().splitlines() if path.exists() else []

        # The flow sees the whole batch, and acknowledged retries never send it twice.
        batch = {"tings": [{"id": "batch-first", "type": "batch", "data": {"message": "batch first"}, "metadata": {}},
                            {"id": "batch-second", "type": "ping", "data": {"message": "batch second"}, "metadata": {}}]}
        post("/events", batch, host="e2e.local.localhost", status=204)
        batch_message = "batch first\nbatch second"
        eventually(lambda: messages("source", batch_message))
        post("/events", batch, host="e2e.local.localhost", status=204)
        eventually(lambda: session("source")["status"] == "idle")
        assert len(messages("source", batch_message)) == 1
        assert not messages("source", "batch first") and not messages("source", "batch second")
        assert len(messages("source", "gated delivery")) == 1

        # Omni 0.9 rotates the provider twice after a context limit while retaining this ISI session.
        before_recovery = session("source")["session_id"]
        control("send", silicon="si:e2e", isi="source", message="overflow context")
        eventually(lambda: any("New session was auto-started due to context limit" in row["message"] for row in messages("source")))
        eventually(lambda: session("source")["status"] == "idle")
        assert session("source")["session_id"] == before_recovery
        recovery_events = control("show", silicon="si:e2e", isi="source")["events"]
        assert any(row["type"] == "error" and row["kind"] == "context_limit" for row in recovery_events), recovery_events
        assert any("Session Limit was hit" in row["message"] and "overflow context" in row["message"] for row in messages("source"))

        si("isi", "send", "pulse", "hold pulse", "--id", "alpha", "--new")
        eventually(lambda: messages("pulse:alpha", "hold pulse"))
        event_log = home / ".silicon/sessions/events" / f"{session('pulse', 'alpha')['session_id']}.jsonl"
        def injected_during_refresh():
            # The UI's last-100-events window can evict an injection while this
            # deliberately busy provider keeps streaming. Inspect durable events.
            complete_lines = event_log.read_bytes().split(b"\n")[:-1]
            return any(event.get("type") == "injected" and event.get("text") == "during refresh"
                       for event in (json.loads(line) for line in complete_lines))
        (home / "slow-refresh").touch()
        try:
            eventually(lambda: (home / "refresh-started").exists())
            (home / "dna-interval").write_text("30s")
            si("isi", "send", "pulse", "during refresh", "--id", "alpha")
            eventually(injected_during_refresh)
            assert not (home / "refresh-finished").exists(), "DNA gate opened before the injection was observed"
        finally:
            # Release a blocked shell even when an assertion fails, before daemon cleanup.
            (home / "slow-refresh").unlink(missing_ok=True)
            (home / "refresh-release").touch()
        eventually(lambda: (home / "refresh-finished").exists())
        eventually(lambda: len(lines("dna-deadlines")) >= 2)
        assemblies = len(lines("dna-assemblies"))
        time.sleep(1)
        assert len(lines("dna-assemblies")) == assemblies, "next_refresh was not reevaluated after assembly"
        assert all(address == "pulse:alpha" for address in lines("dna-deadlines"))
        assert len(lines("dna-assemblies")) >= 2, "busy provider prevented DNA refresh"

        first_suggestion = eventually(lambda: messages("pulse:alpha", "suggest pulse:alpha"))[0]
        assert session("pulse", "alpha")["new_messages"] == 2
        si("isi", "send", "pulse", "cooldown one", "--id", "alpha")
        si("isi", "send", "pulse", "cooldown two", "--id", "alpha")
        # The slow refresh above can consume the first cooldown; establish a fresh boundary.
        suggestions = messages("pulse:alpha", "suggest pulse:alpha")
        if len(suggestions) == 1:
            eventually(lambda: time.time() - first_suggestion["at"] >= 3.1)
            si("isi", "send", "pulse", "after cooldown", "--id", "alpha")
        latest = eventually(lambda: messages("pulse:alpha", "suggest pulse:alpha") if len(messages("pulse:alpha", "suggest pulse:alpha")) == 2 else None)[-1]
        before = session("pulse", "alpha")["new_messages"]
        si("isi", "send", "pulse", "within cooldown one", "--id", "alpha")
        si("isi", "send", "pulse", "within cooldown two", "--id", "alpha")
        # Heartbeats coalesce: one waits while the held turn is open.
        eventually(lambda: len(messages("pulse:alpha", "true")) >= 1)
        assert time.time() - latest["at"] < 3
        assert len(messages("pulse:alpha", "suggest pulse:alpha")) == 2, "suggestion ignored cooldown"
        assert session("pulse", "alpha")["new_messages"] == before + 2, "control messages counted toward suggestion threshold"
        si("isi", "send", "pulse", "one user message", "--id", "beta", "--new")
        eventually(lambda: messages("pulse:beta", "true"))
        beta = session("pulse", "beta")
        assert beta["new_messages"] == 1 and beta["last_suggestion"] is None
        assert not messages("pulse:beta", "suggest pulse:beta"), "heartbeats triggered a suggestion"
        assert {"pulse:alpha", "pulse:beta"} <= set(lines("heartbeat-deadlines"))

        pulse_info = eventually(lambda: next((json.loads(file.read_text()) for file in home.glob("*.provider.json") if json.loads(file.read_text())["ISI"] == "pulse:alpha"), None))
        pulse_env = dict(env, **{key: pulse_info[key] for key in ("SILICON_HOME", "ISI", "SI_URL", "SI_TOKEN", "TZ")})
        si("isi", "send", "pulse", "finish", "--id", "alpha")
        # Once the turn ends, heartbeats resume.
        eventually(lambda: len(messages("pulse:alpha", "true")) >= 2)
        eventually(lambda: session("pulse", "alpha")["status"] == "idle")
        for name, logical_id, context in [("source", None, child_env), ("pulse", "alpha", pulse_env)]:
            old = session(name, logical_id)
            archive_id = name + "-history"
            successor = json.loads(si("--json", "session", "new", "--archive-current-session", "--id", archive_id, "--title", "Finished work", "--description", "Preserved summary", context=context))
            archived = next(row for row in control("sessions", silicon="si:e2e", isi=name, archived=True) if row["id"] == archive_id)
            assert archived["session_id"] == old["session_id"] and archived["first"] == old["first"]
            assert archived["title"] == "Finished work" and archived["description"] == "Preserved summary" and archived["archived_at"]
            assert successor["session_id"] != old["session_id"] and successor["archived_at"] is None
            assert successor["id"] == logical_id if logical_id else successor["id"] != old["id"]
            post("/si", {"action": "sessions", "args": {"isi": name}}, token=context["SI_TOKEN"], status=401)

        # The successor can ask its own archive and receives the answer without self access.
        event({"type": "ping", "data": {"message": "hold successor"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: source_event("hold successor", "start"))
        next_info = eventually(lambda: next((json.loads(file.read_text()) for file in home.glob("*.provider.json") if json.loads(file.read_text())["ISI"] == "source" and json.loads(file.read_text())["SI_TOKEN"] != child_env["SI_TOKEN"]), None))
        next_env = dict(env, **{key: next_info[key] for key in ("SILICON_HOME", "ISI", "SI_URL", "SI_TOKEN", "TZ")})
        si("isi", "send", "source", "archive question", "--archived", "--id", "source-history", context=next_env)
        eventually(lambda: any(event["type"] == "injected" and "source completed:" in event.get("text", "") and "archive question" in event["text"] for event in control("show", silicon="si:e2e", isi="source")["events"]))
        archived_progress = control("show", silicon="si:e2e", isi="source", id="source-history")
        assert any("archive question" in event.get("text", "") for event in archived_progress["events"])
        assert (home / ".silicon/omni" / archived_progress["session"]["session_id"]).is_dir(), "archive query discarded its history"
        event({"type": "ping", "data": {"message": "finish"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: session("source")["status"] == "idle")

        control("send", silicon="si:e2e", isi="target", id="end-after-restart", new=True, message="save before restart")
        eventually(lambda: session("target", "end-after-restart")["status"] == "idle")
        restored = {name: control("sessions", silicon="si:e2e", isi=name) for name in ("source", "pulse")}
        control("shutdown")
        process.wait(timeout=15)
        assert process.returncode == 0, (work / "server.log").read_text()
        assert json.loads((home / "ting-hook.json").read_text())["state"] == "detached", "shutdown must detach Ting before exiting"
        assert not (state / "daemon.json").exists()
        assert config.read_bytes() == original
        # Canonical YAML remains authoritative when the saved routing descriptor predates migration.
        connection_path = state / "connections.json"
        saved_connections = json.loads(connection_path.read_text())
        assert len(saved_connections) == 1 and saved_connections[0]["id"] == "si:e2e"
        saved_connections[0].update(id="e2e:local", host="e2e.local.localhost")
        connection_path.write_text(json.dumps(saved_connections))
        process = subprocess.Popen([str(binary), "serve", "--port", "1823"], env=env, cwd=home, stdout=log, stderr=log)
        daemon = eventually(started)
        base = f'http://127.0.0.1:{daemon["port"]}'
        # Restore runs in the background after the interpreter answers.
        eventually(lambda: [row.get("state") for row in control("list")] == ["connected"])
        assert len(control("list")) == 1
        # The registry file follows the restored state within moments.
        migrated = {**saved_connections[0], "id": "si:e2e", "host": "e2e.local.localhost"}
        eventually(lambda: json.loads(connection_path.read_text()) == [migrated])
        post("/events", batch, host="e2e.local.localhost", status=204)
        assert len(messages("source", batch_message)) == 1
        assert not messages("source", "batch first") and not messages("source", "batch second")
        for name, records in restored.items():
            assert {row["session_id"] for row in control("sessions", silicon="si:e2e", isi=name)} == {row["session_id"] for row in records}
            assert any(row["id"] == name + "-history" for row in control("sessions", silicon="si:e2e", isi=name, archived=True))
        unloaded = session("target", "end-after-restart")
        control("end", silicon="si:e2e", isi="target", id=unloaded["session_id"])
        assert not any(row["id"] == "end-after-restart" for row in control("sessions", silicon="si:e2e", isi="target"))
        assert any(row["session_id"] == unloaded["session_id"] for row in control("sessions", silicon="si:e2e", isi="target", archived=True))
        event({"type": "ping", "data": {"message": "after restart"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: messages("source", "after restart"))
        eventually(lambda: session("source")["status"] == "idle")
        assert len(messages("source", batch_message)) == 1
        assert not messages("source", "batch first") and not messages("source", "batch second")
        assert config.read_bytes() == original
        cli("disconnect", "si:e2e")
        assert json.loads((home / "ting-hook.json").read_text())["state"] == "detached"
        assert not control("list")
        assert config.read_bytes() == original
        control("shutdown")
        process.wait(timeout=15)
        assert process.returncode == 0, (work / "server.log").read_text()
        assert not (state / "daemon.json").exists()
        print("E2E passed: si app install/config/uninstall, org propagation, implicit Ting install/auth/register, durable generic JSON/batch acknowledgement, aggregation, dedup/reconnect, Omni context recovery, setup output/exactly-once, settings, redacted configuration, local ping, bug report preview, port fallback, HTTP validation, relative-path disconnect isolation, live injection, ISI context/access, archives, ephemeral reply, heartbeat, suggestion limits, busy DNA refresh, session rollover, restart restore, disconnect, shutdown.")
    finally:
        (home / "flow-release").touch()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        occupied.close()
        registry.shutdown()
        log.close()


if __name__ == "__main__":
    provider() if Path(sys.argv[0]).name == "claude" else main()
