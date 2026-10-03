#!/usr/bin/env python3
"""Run with python3 scripts/test-migrate-identifiers.py; temporary homes only."""
import fcntl
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace


with tempfile.TemporaryDirectory() as root:
    root = Path(root)
    home, interpreter = root / "home", root / "interpreter"
    old = home / ".silicon/ting/assistant:owner"
    new = home / ".silicon/ting/si:assistant"
    state = home / ".silicon"
    (old / "selected").mkdir(parents=True)
    (old / "another-org").mkdir()
    payload = b'{"seen":{"event-id":123},"pending":[{"data":"tos>dm assistant:owner"}]}\n'
    (old / "selected/inbox.json").write_bytes(payload)
    (old / "selected/hook.json").write_bytes(b'"stable-hook-id"\n')
    (old / "another-org/inbox.json").write_bytes(payload)
    (state / "org.json").write_text('"selected"')
    (state / "auth-checked.json").write_bytes(b'{"old-key":123}')
    (state / "auth-grants.json").write_bytes(b'{"old-key":"selected"}')
    explicit = '/old/path/tos>dm --message "tos>dm"'
    (state / "auth-apps.json").write_text(json.dumps(["tos>dm", explicit, "helper", "dm"]))
    (state / "sessions").mkdir()
    (state / "sessions/session.json").write_bytes(b'{"id":"retained-session"}')
    (home / "silicon.yaml").write_bytes(b"token: retained-secret\n")
    mapping = root / "map.json"
    data = {
        "applications": [{"legacy_id": "tos>dm", "app_id": "dm", "org_id": "tos"}],
        "identities": [
            {"legacy_id": "assistant:owner", "public_id": "si:assistant", "org_id": "owner"},
            {"legacy_id": "assistant:owner", "public_id": "si:testing", "testing_environment_id": "other-world"},
        ],
    }
    mapping.write_text(json.dumps(data))
    command = [sys.executable, str(Path(__file__).with_name("migrate-identifiers.py")),
               "--home", str(home), "--map", str(mapping), "--old-id", "assistant:owner",
               "--org-id", "selected", "--interpreter-home", str(interpreter),
               "--command", "helper", "--command", "dm"]

    def run(*args, succeeds=True):
        result = subprocess.run(command + list(args), capture_output=True, text=True)
        assert (result.returncode == 0) == succeeds, result.stderr + result.stdout
        return result

    def snapshot():
        return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}

    before = snapshot()
    ambiguous = subprocess.run(command[:-4], capture_output=True, text=True)
    assert ambiguous.returncode != 0 and "ambiguous" in ambiguous.stderr
    run()
    assert snapshot() == before
    run("--apply", succeeds=False)
    run("--org-id", "owner", succeeds=False)
    run("--org-id", "selected-", succeeds=False)
    (state / "org.json").write_text('"_selected-org_"')
    run("--org-id", "_selected-org_")
    (state / "org.json").write_text('"selected"')
    run("--org-id", "ab", succeeds=False)
    run("--org-id", "SELECTED", succeeds=False)
    run("--command", "missing", succeeds=False)
    run("--testing-environment-id", "missing-world", succeeds=False)
    new.mkdir()
    run(succeeds=False)
    new.rmdir()
    data["applications"] = []
    mapping.write_text(json.dumps(data))
    run(succeeds=False)
    data["applications"] = [{"legacy_id": "tos>dm", "app_id": "dm", "org_id": "tos"}]
    data["identities"][0]["kind"] = "carbon"
    mapping.write_text(json.dumps(data))
    run(succeeds=False)
    data["identities"][0]["kind"] = "silicon"
    data["identities"][0]["org_id"] = "selected"
    mapping.write_text(json.dumps(data))
    run(succeeds=False)
    data["identities"][0]["org_id"] = "owner"
    mapping.write_text(json.dumps(data))
    (old.parent / "unknown:owner").mkdir()
    run(succeeds=False)
    (old.parent / "unknown:owner").rmdir()
    data["identities"].append({"legacy_id": "other:owner", "public_id": "si:other"})
    mapping.write_text(json.dumps(data))
    (old.parent / "si:other").mkdir()
    run(succeeds=False)
    (old.parent / "si:other").rmdir()
    (old / "link").symlink_to(home / "silicon.yaml")
    run(succeeds=False)
    (old / "link").unlink()
    with (interpreter / "daemon.lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(succeeds=False)
    assert snapshot() == before

    # A corrupted backup or concurrent source change must be detected before any rewrite.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("migration", command[1])
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    args = SimpleNamespace(home=home, map=mapping, old_id="assistant:owner", org_id="selected",
                           testing_environment_id=None, command=["helper", "dm"], apply=True,
                           backup_dir=root / "refused-backups")
    original_copy = migration.shutil.copy2
    for tamper_source in [False, True]:
        changed = []
        def corrupt_copy(source, destination, *args, **kwargs):
            result = original_copy(source, destination, *args, **kwargs)
            if not changed:
                path = Path(source if tamper_source else destination)
                path.write_bytes(path.read_bytes() + b"\n")
                changed.append(path)
            return result
        migration.shutil.copy2 = corrupt_copy
        try:
            migration.migrate(args)
            raise AssertionError("corrupt backup or changed source was accepted")
        except ValueError as error:
            assert "source changed" in str(error) if tamper_source else "backup verification failed" in str(error)
        finally:
            migration.shutil.copy2 = original_copy
        if tamper_source:
            (state / "auth-apps.json").write_bytes(before[".silicon/auth-apps.json"])
        assert snapshot() == before

    run("--apply", "--backup-dir", str(root / "backups"))
    assert not old.exists()
    assert (new / "selected/inbox.json").read_bytes() == payload
    assert (new / "another-org/inbox.json").read_bytes() == payload
    assert (new / "selected/hook.json").read_bytes() == b'"stable-hook-id"\n'
    assert json.loads((state / "auth-apps.json").read_bytes()) == ["dm", explicit, "! helper", "! dm"]
    assert not (state / "auth-checked.json").exists()
    assert not (state / "auth-grants.json").exists()
    assert (state / "sessions/session.json").read_bytes() == before[".silicon/sessions/session.json"]
    assert (home / "silicon.yaml").read_bytes() == before["silicon.yaml"]
    backup, = (root / "backups").iterdir()
    for path, contents in before.items():
        saved = backup / path
        if saved.exists():
            assert saved.read_bytes() == contents
    assert (backup / ".silicon/auth-checked.json").read_bytes() == before[".silicon/auth-checked.json"]
    after = snapshot()
    run("--apply", "--backup-dir", str(root / "backups"))
    assert snapshot() == after

print("identifier migration checks passed")
