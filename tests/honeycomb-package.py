"""Exercise complete packaging plus archive and release-integrity failures."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import zipfile

spec = importlib.util.spec_from_file_location("pack", Path(__file__).parents[1] / "scripts/prepare_honeycomb.py")
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    checksums = []
    for target, rust_target in pack.TARGETS.items():
        windows = target.startswith("windows-")
        archive = root / f"silicon-{rust_target}.{'zip' if windows else 'tar.gz'}"
        payload = {"VERSION": b"v4.0.0\n", "LICENSE": b"test license"}
        payload.update({f"{command}.exe" if windows else f"bin/{command}":
                        b"MZfixture" if windows else b"#!/bin/sh\nexit 0\n"
                        for command in pack.COMMANDS})
        if windows:
            payload["runtime.tar.gz"] = b"matching Linux payload placeholder"
            with zipfile.ZipFile(archive, "w") as bundle:
                for name, data in payload.items():
                    bundle.writestr(name, data)
        else:
            with tarfile.open(archive, "w:gz") as bundle:
                for name, data in payload.items():
                    member = tarfile.TarInfo(name)
                    member.mode = 0o755 if name.startswith("bin/") else 0o644
                    member.size = len(data)
                    bundle.addfile(member, io.BytesIO(data))
        checksums.append(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}")
    (root / "SHA256SUMS").write_text("\n".join(checksums))
    pack.prepare(root, root / "package", "4.0.0")
    manifest = json.loads((root / "package/honeycomb.yaml").read_text())
    assert set(manifest["targets"]) == set(pack.TARGETS)
    assert set(manifest["bin"]) == set(pack.COMMANDS)
    assert (root / "package/targets/windows-aarch64/runtime.tar.gz").read_bytes() == payload["runtime.tar.gz"]
    if binary := os.environ.get("HONEYCOMB_TEST_BIN"):
        env = dict(os.environ, SILICON_HOME=str(root / "honeycomb-home"), HONEYCOMB_TELEMETRY="0")
        def honeycomb(*args):
            result = subprocess.run([binary, *map(str, args), "--json"], env=env,
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        honeycomb("config", "set", "auto_update", "false")
        assert honeycomb("validate", root / "package")["valid"]
        honeycomb("pack", root / "package", "--output", root / "package.tar.gz")
        assert honeycomb("validate", root / "package.tar.gz")["valid"]
    for version in ["4.0.1", "4.0.0"]:
        if version == "4.0.0":
            (root / "SHA256SUMS").write_text("\n".join(checksums).replace(checksums[0][:64], "0" * 64))
        try:
            pack.prepare(root, root / "rejected", version)
        except ValueError:
            assert not (root / "rejected").exists()
        else:
            raise AssertionError("Invalid release accepted")
    archive = root / "silicon-x86_64-unknown-linux-gnu.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        bundle.addfile(member, io.BytesIO(b"x"))
    checksums[0] = f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}"
    (root / "SHA256SUMS").write_text("\n".join(checksums))
    try:
        pack.prepare(root, root / "rejected", "4.0.0")
    except ValueError:
        assert not (root / "escape").exists()
    else:
        raise AssertionError("Traversal archive accepted")
print("Honeycomb package preparation checks passed")
