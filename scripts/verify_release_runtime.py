#!/usr/bin/env python3
"""Fail closed unless the Stemcell metadata and runtime image agree."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from silicon_cli import runtime_contract


IMAGE_RE = re.compile(
    r"\A[a-z0-9][a-z0-9._:/-]{0,438}@sha256:[0-9a-f]{64}\Z"
)
RESULT_MARKER = "SILICON_RELEASE_GATE="


def _require_dependency_alignment(root: Path, declared: dict) -> None:
    expected = str(declared.get("silicon_extend") or "")
    if not expected:
        raise RuntimeError("runtime contract has no Silicon Extend version")
    pattern = re.compile(r"(?m)^silicon-extend==([^\s;\\]+)")
    for filename in ("requirements.txt", "requirements.lock"):
        path = root / filename
        versions = set(pattern.findall(path.read_text(encoding="utf-8")))
        if versions != {expected}:
            found = ", ".join(sorted(versions)) or "missing"
            raise RuntimeError(
                f"{filename} Silicon Extend version does not match the "
                f"runtime contract: found {found}, require {expected}"
            )


def _run(command: list[str]) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, time.monotonic() - started


def _require_success(
    result: subprocess.CompletedProcess[str],
    *,
    action: str,
) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "no output").strip()
    raise RuntimeError(f"{action} failed: {detail[-4000:]}")


def _probe_payload(output: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("runtime probe returned no structured inventory")


def main() -> int:
    total_started = time.monotonic()
    root = Path(__file__).resolve().parents[1]
    info = json.loads((root / "silicon.info").read_text(encoding="utf-8"))
    version = str(info.get("version") or "")
    image = str(info.get("runtime_image") or "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RuntimeError("silicon.info has an invalid release version")
    if IMAGE_RE.fullmatch(image) is None:
        raise RuntimeError("silicon.info has no immutable runtime image digest")

    ref_type = os.environ.get("GITHUB_REF_TYPE", "")
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    if ref_type == "tag" and ref_name != f"v{version}":
        raise RuntimeError(
            f"Git tag {ref_name!r} does not match silicon.info version {version!r}"
        )

    metadata_started = time.monotonic()
    declared = runtime_contract.verify_release_contract_metadata(
        info.get("runtime_contract")
    )
    _require_dependency_alignment(root, declared)
    metadata_seconds = time.monotonic() - metadata_started

    pull, pull_seconds = _run(["docker", "pull", image])
    _require_success(pull, action="immutable runtime image pull")

    label, label_seconds = _run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "io.teamofsilicons.runtime-contract-sha256"}}',
            image,
        ]
    )
    _require_success(label, action="runtime contract label inspection")
    if label.stdout.strip() != declared["sha256"]:
        raise RuntimeError(
            "runtime image contract label does not match silicon.info: "
            f"found {label.stdout.strip() or 'missing'}, "
            f"require {declared['sha256']}"
        )

    probe, probe_seconds = _run(
        [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "--entrypoint",
            "/opt/silicon-runtime/bin/python",
            image,
            "-I",
            "-c",
            runtime_contract.DOCKER_PROBE_SCRIPT,
            runtime_contract.docker_contract_json(),
        ]
    )
    payload = _probe_payload(probe.stdout)
    failures = payload.get("failures")
    if probe.returncode != 0 or not isinstance(failures, list) or failures:
        detail = "; ".join(str(item) for item in failures or [])
        raise RuntimeError(
            "runtime image does not satisfy its declared contract: "
            + (detail or probe.stderr.strip() or "probe failed")
        )

    result = {
        "schema": 1,
        "status": "succeeded",
        "version": version,
        "runtime_image": image,
        "runtime_contract_sha256": declared["sha256"],
        "versions": payload.get("versions", {}),
        "timings_seconds": {
            "metadata": round(metadata_seconds, 3),
            "image_pull": round(pull_seconds, 3),
            "image_label": round(label_seconds, 3),
            "runtime_probe": round(probe_seconds, 3),
            "total": round(time.monotonic() - total_started, 3),
        },
    }
    print(RESULT_MARKER + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"release runtime verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
