#!/usr/bin/env python3
"""Fast source-only gate for a published host-local Stemcell release."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path


IMAGE_RE = re.compile(
    r"\A[a-z0-9][a-z0-9._:/-]{0,438}@sha256:[0-9a-f]{64}\Z"
)
RESULT_MARKER = "SILICON_RELEASE_GATE="


def main() -> int:
    started = time.monotonic()
    root = Path(__file__).resolve().parents[1]
    info_path = root / "silicon.info"
    metadata = info_path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > 64 * 1024
    ):
        raise RuntimeError("silicon.info is not a bounded regular file")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if not isinstance(info, dict):
        raise RuntimeError("silicon.info must contain one JSON object")
    version = str(info.get("version") or "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise RuntimeError("silicon.info has an invalid release version")

    ref_type = os.environ.get("GITHUB_REF_TYPE", "")
    ref_name = os.environ.get("GITHUB_REF_NAME", "")
    if ref_type == "tag" and ref_name != f"v{version}":
        raise RuntimeError(
            f"Git tag {ref_name!r} does not match silicon.info version {version!r}"
        )

    # This frozen digest is read only by already-registered legacy Docker
    # Silicons. Validate its shape without pulling it or putting it on the
    # host-local release critical path.
    image = str(info.get("runtime_image") or "")
    if image and IMAGE_RE.fullmatch(image) is None:
        raise RuntimeError("silicon.info has an invalid compatibility image digest")
    contract = info.get("runtime_contract", {})
    if not isinstance(contract, dict):
        raise RuntimeError("silicon.info runtime_contract must be an object")

    result = {
        "schema": 1,
        "status": "succeeded",
        "runtime": "host-local",
        "version": version,
        "compatibility_image": image,
        "total_seconds": round(time.monotonic() - started, 3),
    }
    print(RESULT_MARKER + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
