"""Authoritative code and instance-data roots for the Stemcell runtime.

Transactional updates execute Python from an immutable release generation
under ``.silicon/releases`` while keeping all durable state in the original
Silicon instance directory.  Runtime modules must use :data:`CODE_ROOT` for
tracked source/templates and :data:`DATA_ROOT` for anything that can change
while Silicon is running.

Legacy flat installations do not set ``SILICON_DATA_ROOT``.  For those
installations both roots intentionally resolve to the checkout root.
"""
from __future__ import annotations

import os
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT_ENV = "SILICON_DATA_ROOT"
RELEASE_ROOT_ENV = "SILICON_RELEASE_ROOT"
DATA_ROOT_CAPABILITY = ".silicon-data-root-v1"


class RuntimePathError(RuntimeError):
    """A configured runtime root is unsafe or internally inconsistent."""


def _inside_release_store(path: Path) -> bool:
    parts = path.parts
    return any(
        parts[index : index + 2] == (".silicon", "releases")
        for index in range(max(0, len(parts) - 1))
    )


def validated_data_root(
    value: str | os.PathLike[str] | None = None,
) -> Path:
    """Return a canonical, existing mutable instance root.

    An explicitly configured root must be absolute, exist, be a directory, and
    not itself live inside the immutable release store.  Resolving symlinks
    here gives every module one filesystem identity for locks and state files.
    """

    raw = str(
        value if value is not None else os.environ.get(DATA_ROOT_ENV, "")
    ).strip()
    if not raw:
        return CODE_ROOT
    if "\x00" in raw:
        raise RuntimePathError(f"{DATA_ROOT_ENV} contains an invalid NUL byte")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise RuntimePathError(f"{DATA_ROOT_ENV} must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimePathError(f"{DATA_ROOT_ENV} does not exist") from exc
    if not resolved.is_dir():
        raise RuntimePathError(f"{DATA_ROOT_ENV} must identify a directory")
    if resolved == Path(resolved.anchor):
        raise RuntimePathError(f"{DATA_ROOT_ENV} cannot be a filesystem root")
    if _inside_release_store(resolved):
        raise RuntimePathError(
            f"{DATA_ROOT_ENV} cannot point inside .silicon/releases"
        )
    return resolved


DATA_ROOT = validated_data_root()


def resolve_data_relative(value: str | os.PathLike[str]) -> Path:
    """Resolve a configured data path, making relative values instance-local."""

    path = Path(value).expanduser()
    return path if path.is_absolute() else DATA_ROOT / path
