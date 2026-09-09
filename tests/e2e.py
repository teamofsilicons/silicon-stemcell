#!/usr/bin/env python3
"""Real interpreter + Omni + Caddy; scripted Claude protocol avoids paid model calls.
Run after cargo build: OMNI_DAEMON=/path/omnid SILICON_CADDY=/path/caddy python3 tests/e2e.py
"""
import http.server
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
    home = work / "silicon"
    home.mkdir()
    (home / "dna-interval").write_text("0.5s")
    state = work / "interpreter"
    state.mkdir()
    registry = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Registry)
    threading.Thread(target=registry.serve_forever, daemon=True).start()
    env = dict(os.environ, SILICON_INTERPRETER_HOME=str(state), PATH=str(binaries) + os.pathsep + str(binary_dir) + os.pathsep + os.environ["PATH"], OMNI_REGISTRY=f"http://127.0.0.1:{registry.server_port}/choose.json", SILICON_AUTO_UPDATE="0")
    assert shutil.which(env.get("OMNI_DAEMON", "omnid"), path=env["PATH"]), "set OMNI_DAEMON to the real Omni daemon"
    assert shutil.which(env.get("SILICON_CADDY", "caddy"), path=env["PATH"]), "set SILICON_CADDY to real Caddy"
    config = home / "silicon.yaml"
    config.write_text(f'''silicon:
  id: e2e:local
  token: isolated-e2e-token
  timezone: Asia/Kolkata
  SILICON_HOME: {json.dumps(str(home))}
  inference_providers: [claude-code-cli]
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
        - '! printf "%s\\n" "$ISI" >> "$SILICON_HOME/dna-assemblies"; if test -f "$SILICON_HOME/slow-refresh"; then touch "$SILICON_HOME/refresh-started"; sleep 3; fi; printf "pulse DNA for %s" "$ISI"'
      next_refresh: '! printf "%s\\n" "$ISI" >> "$SILICON_HOME/dna-deadlines"; cat "$SILICON_HOME/dna-interval"'
    heartbeat:
      next: '! printf "%s\\n" "$ISI" >> "$SILICON_HOME/heartbeat-deadlines"; printf 0.5s'
      message: 'true'
    new_session_suggestion:
      min_new_messages: 2
      cooldown_minutes: 3s
      suggestion_message: '! printf "suggest %s" "$ISI"'
access:
  source: [target, ephemeral, pulse]
  target: [source]
  ephemeral: [source]
  restricted: []
  pulse: [source]
flow:
  - var:
      name: payload
      value: '{{request.data}}'
  - send:
      isi: source
      message: '{{var.payload.message}}'
  - log:
      message: 'flow finished {{request.type}}'
''')
    original = config.read_bytes()
    binary = binary_dir / "silicon"
    def cli(*args, ok=True, child_env=env):
        out = subprocess.run([str(binary), *args], env=child_env, capture_output=True, text=True, timeout=35)
        assert (out.returncode == 0) == ok, out.stdout + out.stderr
        return out.stdout
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 1823))
    occupied.listen()
    log = (work / "server.log").open("w")
    process = subprocess.Popen([str(binary), "serve", "--port", "1823"], env=env, stdout=log, stderr=log)
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
            result = json.loads(response.read())
            assert response.status == status, (response.status, result)
            return result
        def control(action, **args):
            return post("/control", {"action": action, "args": args}, daemon["token"])
        cli("compile", str(config))
        cli("connect", str(config))
        assert "e2e:local" in cli("ls", "*:local")
        assert len(control("list")) == 1
        post("/control", {"action": "list", "args": {}}, status=401)
        post("/", {"type": "ping", "data": [], "metadata": {}}, host="e2e.local.localhost", status=400)
        post("/", {"type": "ping", "data": {}, "metadata": {}}, host="other.local.localhost", status=404)
        start = time.monotonic()
        post("/", {"type": "ping", "data": {"message": "hold first"}, "metadata": {}}, host="e2e.local.localhost")
        assert time.monotonic() - start < 20, "event waited for model completion"
        record = control("sessions", silicon="e2e:local", isi="source")[0]
        assert record["status"] == "running"
        post("/", {"type": "ping", "data": {"message": "mid-turn"}, "metadata": {}}, host="e2e.local.localhost")
        progress = control("show", silicon="e2e:local", isi="source")
        assert any(event["type"] == "injected" and event["text"] == "mid-turn" for event in progress["events"]), progress
        assert not any(event["type"] == "end" for event in progress["events"])
        provider_info = eventually(lambda: next((json.loads(file.read_text()) for file in home.glob("*.provider.json") if json.loads(file.read_text())["ISI"] == "source"), None))
        assert provider_info["SILICON_HOME"] == str(home)
        assert provider_info["cwd"] == str(home)
        assert provider_info["TZ"] == "Asia/Kolkata"
        prompt = provider_info["argv"][provider_info["argv"].index("--system-prompt") + 1]
        assert "isi=source" in prompt and "target (session)" in prompt and "restricted" not in prompt
        child_env = dict(env, **{key: provider_info[key] for key in ("SILICON_HOME", "ISI", "SI_URL", "SI_TOKEN", "TZ")})
        def si(*args, ok=True, context=child_env):
            result = subprocess.run([str(binary_dir / "si"), *args], env=context, capture_output=True, text=True, timeout=35)
            assert (result.returncode == 0) == ok, result.stdout + result.stderr
            return result.stdout
        si("isi", "send", "restricted", "forbidden", ok=False)
        si("isi", "send", "target", "missing", "--id", "missing", ok=False)
        si("isi", "send", "target", "hold target", "--id", "job", "--new", "--title", "A job")
        assert control("sessions", silicon="e2e:local", isi="target")[0]["id"] == "job"
        si("isi", "send", "target", "finish", "--id", "job")
        eventually(lambda: control("sessions", silicon="e2e:local", isi="target")[0]["status"] == "idle")
        si("isi", "end", "target", "--id", "job")
        assert not control("sessions", silicon="e2e:local", isi="target")
        archive = control("sessions", silicon="e2e:local", isi="target", archived=True)[0]
        assert archive["id"] == "job" and archive["archived_at"]
        si("isi", "send", "target", "history question", "--id", "job", "--archived")
        si("isi", "send", "ephemeral", "short task")
        eventually(lambda: not control("sessions", silicon="e2e:local", isi="ephemeral"))
        eventually(lambda: any(event["type"] == "injected" and "ephemeral completed:" in event["text"] for event in control("show", silicon="e2e:local", isi="source")["events"]))
        post("/", {"type": "ping", "data": {"message": "finish"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: control("sessions", silicon="e2e:local", isi="source")[0]["status"] == "idle")

        def messages(address, text=None):
            path = home / "provider-messages.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            return [row for row in rows if row["isi"] == address and (text is None or row["message"] == text)]
        def session(isi, session_id=None):
            return next(row for row in control("sessions", silicon="e2e:local", isi=isi) if session_id is None or row["id"] == session_id)
        def lines(name):
            path = home / name
            return path.read_text().splitlines() if path.exists() else []

        si("isi", "send", "pulse", "hold pulse", "--id", "alpha", "--new")
        eventually(lambda: messages("pulse:alpha", "hold pulse"))
        (home / "slow-refresh").touch()
        eventually(lambda: (home / "refresh-started").exists())
        (home / "dna-interval").write_text("30s")
        start = time.monotonic()
        si("isi", "send", "pulse", "during refresh", "--id", "alpha")
        eventually(lambda: any(event["type"] == "injected" and event["text"] == "during refresh" for event in control("show", silicon="e2e:local", isi="pulse", id="alpha")["events"]), timeout=2)
        assert time.monotonic() - start < 2.5, "DNA shell blocked model event processing"
        eventually(lambda: len(lines("dna-deadlines")) >= 2)
        (home / "slow-refresh").unlink()
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
        eventually(lambda: len(messages("pulse:alpha", "true")) >= 2)
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
        eventually(lambda: session("pulse", "alpha")["status"] == "idle")
        for name, logical_id, context in [("source", None, child_env), ("pulse", "alpha", pulse_env)]:
            old = session(name, logical_id)
            archive_id = name + "-history"
            successor = json.loads(si("--json", "session", "new", "--archive-current-session", "--id", archive_id, "--title", "Finished work", "--description", "Preserved summary", context=context))
            archived = next(row for row in control("sessions", silicon="e2e:local", isi=name, archived=True) if row["id"] == archive_id)
            assert archived["session_id"] == old["session_id"] and archived["first"] == old["first"]
            assert archived["title"] == "Finished work" and archived["description"] == "Preserved summary" and archived["archived_at"]
            assert successor["session_id"] != old["session_id"] and successor["archived_at"] is None
            assert successor["id"] == logical_id if logical_id else successor["id"] != old["id"]
            post("/si", {"action": "sessions", "args": {"isi": name}}, token=context["SI_TOKEN"], status=401)

        # The successor can ask its own archive and receives the answer without self access.
        post("/", {"type": "ping", "data": {"message": "hold successor"}, "metadata": {}}, host="e2e.local.localhost")
        next_info = eventually(lambda: next((json.loads(file.read_text()) for file in home.glob("*.provider.json") if json.loads(file.read_text())["ISI"] == "source" and json.loads(file.read_text())["SI_TOKEN"] != child_env["SI_TOKEN"]), None))
        next_env = dict(env, **{key: next_info[key] for key in ("SILICON_HOME", "ISI", "SI_URL", "SI_TOKEN", "TZ")})
        si("isi", "send", "source", "archive question", "--archived", "--id", "source-history", context=next_env)
        eventually(lambda: any(event["type"] == "injected" and "source completed:" in event.get("text", "") and "archive question" in event["text"] for event in control("show", silicon="e2e:local", isi="source")["events"]))
        archived_progress = control("show", silicon="e2e:local", isi="source", id="source-history")
        assert any("archive question" in event.get("text", "") for event in archived_progress["events"])
        assert (home / ".silicon/omni" / archived_progress["session"]["session_id"]).is_dir(), "archive query discarded its history"
        post("/", {"type": "ping", "data": {"message": "finish"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: session("source")["status"] == "idle")

        control("send", silicon="e2e:local", isi="target", id="end-after-restart", new=True, message="save before restart")
        eventually(lambda: session("target", "end-after-restart")["status"] == "idle")
        restored = {name: control("sessions", silicon="e2e:local", isi=name) for name in ("source", "pulse")}
        control("shutdown")
        process.wait(timeout=15)
        assert process.returncode == 0, (work / "server.log").read_text()
        assert not (state / "daemon.json").exists()
        assert config.read_bytes() == original
        process = subprocess.Popen([str(binary), "serve", "--port", "1823"], env=env, stdout=log, stderr=log)
        daemon = eventually(started)
        base = f'http://127.0.0.1:{daemon["port"]}'
        assert len(control("list")) == 1
        for name, records in restored.items():
            assert {row["session_id"] for row in control("sessions", silicon="e2e:local", isi=name)} == {row["session_id"] for row in records}
            assert any(row["id"] == name + "-history" for row in control("sessions", silicon="e2e:local", isi=name, archived=True))
        unloaded = session("target", "end-after-restart")
        control("end", silicon="e2e:local", isi="target", id=unloaded["session_id"])
        assert not any(row["id"] == "end-after-restart" for row in control("sessions", silicon="e2e:local", isi="target"))
        assert any(row["session_id"] == unloaded["session_id"] for row in control("sessions", silicon="e2e:local", isi="target", archived=True))
        post("/", {"type": "ping", "data": {"message": "after restart"}, "metadata": {}}, host="e2e.local.localhost")
        eventually(lambda: session("source")["status"] == "idle")
        assert config.read_bytes() == original
        cli("disconnect", "e2e:local")
        assert not control("list")
        assert config.read_bytes() == original
        control("shutdown")
        process.wait(timeout=15)
        assert process.returncode == 0, (work / "server.log").read_text()
        assert not (state / "daemon.json").exists()
        print("E2E passed: port fallback, HTTP validation, live injection, ISI context/access, archives, ephemeral reply, heartbeat, suggestion limits, busy DNA refresh, session rollover, restart restore, disconnect, shutdown.")
    finally:
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
