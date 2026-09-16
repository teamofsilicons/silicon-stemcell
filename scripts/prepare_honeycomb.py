#!/usr/bin/env python3
"""Prepare a Honeycomb directory from the six checksummed release archives.

Requires Python 3.12+. Does not register an IAM app, upload, or publish anything.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
import zipfile

TARGETS = {
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-aarch64": "aarch64-unknown-linux-gnu",
    "macos-x86_64": "x86_64-apple-darwin",
    "macos-aarch64": "aarch64-apple-darwin",
    "windows-x86_64": "x86_64-pc-windows-msvc",
    "windows-aarch64": "aarch64-pc-windows-msvc",
}
COMMANDS = "silicon si omnid silicon-omni omni so caddy iam honeycomb spacestation dm briefcase waveform commit remind hook".split()


def prepare(artifacts, output, version):
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    checksums = {}
    for line in (artifacts / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        checksums[name.lstrip("*")] = digest
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        root = Path(temp) / "package"
        root.mkdir()
        manifest = {"format_version": 1, "app_id": "tos>silicon", "version": version,
                    "bin": {command: command for command in ("silicon", "si")}, "targets": {}}
        expanded = 0
        for target, rust_target in TARGETS.items():
            windows = target.startswith("windows-")
            archive = artifacts / f"silicon-{rust_target}.{'zip' if windows else 'tar.gz'}"
            with archive.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if checksums.get(archive.name) != digest:
                raise ValueError(f"Checksum mismatch or missing: {archive.name}")
            dest = root / "targets" / target
            dest.mkdir(parents=True)
            seen = set()
            with zipfile.ZipFile(archive) if windows else tarfile.open(archive) as bundle:
                entries = bundle.infolist() if windows else bundle.getmembers()
                for entry in entries:
                    name = entry.filename if windows else entry.name
                    path = PurePosixPath(name)
                    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                        raise ValueError(f"Unsafe archive path: {name}")
                    normalized = str(path).casefold()
                    if normalized in seen:
                        raise ValueError(f"Duplicate archive path: {name}")
                    seen.add(normalized)
                    if windows:
                        mode = entry.external_attr >> 16
                        if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                            raise ValueError(f"Unsupported archive entry: {name}")
                        expanded += entry.file_size
                    else:
                        if not (entry.isfile() or entry.isdir()):
                            raise ValueError(f"Unsupported archive entry: {name}")
                        expanded += entry.size
                if expanded > 2 * 1024**3:
                    raise ValueError("Honeycomb package exceeds 2 GiB expanded limit")
                if windows:
                    bundle.extractall(dest)
                else:
                    bundle.extractall(dest, filter="data")
            if (dest / "VERSION").read_text().strip().removeprefix("v") != version:
                raise ValueError(f"Release version mismatch: {archive.name}")
            executables = {command: f"{command}.exe" if windows else f"bin/{command}"
                           for command in COMMANDS}
            for relative in executables.values():
                executable = dest / relative
                if not executable.is_file() or executable.stat().st_size == 0:
                    raise ValueError(f"Missing executable: {target}/{relative}")
                if not windows and not executable.stat().st_mode & 0o111:
                    raise ValueError(f"Nonexecutable Unix command: {target}/{relative}")
            manifest["targets"][target] = {"root": f"targets/{target}",
                                            "executables": {command: executables[command]
                                                            for command in manifest["bin"]}}
        # JSON is a YAML subset and avoids a packaging-only YAML dependency.
        (root / "honeycomb.yaml").write_text(json.dumps(manifest, indent=2) + "\n")
        shutil.move(str(root), output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    prepare(args.artifacts, args.output, args.version)
    print(f"Prepared {args.output}; run honeycomb validate, then honeycomb pack.")
