"""Canonical ownership and recovery policy for mutable Silicon data.

The policy is deliberately code-owned: an installation may *add* protected
paths, but it cannot remove the mandatory recovery classes.  The legacy
``.backupsilicon`` file is treated as another additive source so older
installations gain the stronger defaults without losing their custom entries.

Path expansion is implemented here instead of with :mod:`glob`.  It never
follows symbolic links and rejects special files, absolute paths, traversal,
and attempts to include the snapshot store itself.
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping, Sequence

POLICY_SCHEMA = 1
POLICY_FILE = Path(".silicon") / "data-policy.json"
LEGACY_POLICY_FILE = ".backupsilicon"
SNAPSHOT_STORE = Path(".silicon") / "snapshots"


class DataPolicyError(ValueError):
    """The recovery policy or one of its paths is unsafe."""


class UnsafePathError(DataPolicyError):
    """A protected path is not a confined regular file or directory."""


@dataclass(frozen=True)
class OwnershipClass:
    """One mandatory data class and its built-in recovery behavior."""

    name: str
    patterns: tuple[str, ...]
    snapshot: bool = True
    description: str = ""


@dataclass(frozen=True)
class ProtectedFile:
    """A confined regular file and the ownership classes that selected it."""

    path: Path
    relative_path: str
    classes: tuple[str, ...]


# Keep these ordered.  Ordering is part of deterministic policy resolution and
# makes generated manifests stable across platforms.
MANDATORY_CLASSES: tuple[OwnershipClass, ...] = (
    OwnershipClass(
        "security_state",
        (
            ".silicon/release-sequence-floor.json",
        ),
        description=(
            "Monotonic anti-rollback state for authenticated release updates."
        ),
    ),
    OwnershipClass(
        "critical_living",
        (
            "prompts/MEMORY.md",
            "prompts/memory/**",
            "prompts/LORE.md",
            "prompts/CONTACTS.md",
            "silicon.json",
            ".backupsilicon",
            ".silicon/data-policy.json",
        ),
        description="Identity, memories, contacts, lore, and instance settings.",
    ),
    OwnershipClass(
        "task_delivery",
        (
            "core/interface_state/**",
            "core/cron/checkbacks.json",
            "core/cron/history.json",
            ".silicon-interface/**",
            "sessions/**",
            "worker/sessions/**",
        ),
        description="Durable queues, cursors, task state, and provider sessions.",
    ),
    OwnershipClass(
        "self_customization",
        (
            "prompts/**/*.md",
            "extensions/**",
            "skills/**",
            ".silicon/overlays/**",
        ),
        description="Silicon-authored prompts and supported extensions.",
    ),
    OwnershipClass(
        "artifacts",
        (
            "logs/**",
            "worker/outputs/**",
        ),
        description="Work evidence, diagnostics, and deliverables.",
    ),
    OwnershipClass(
        "credentials",
        (
            ".glass.json",
            ".env",
            ".env.*",
        ),
        snapshot=False,
        description="Reissued or encrypted separately; never copied in plaintext.",
    ),
)

_CLASS_BY_NAME: Mapping[str, OwnershipClass] = MappingProxyType(
    {item.name: item for item in MANDATORY_CLASSES}
)
_GLOB_MAGIC = frozenset("*?[")
_FORBIDDEN_PREFIXES = (
    ".git",
    ".silicon/snapshots",
)
_KNOWN_SECRET_NAMES = frozenset(
    {
        ".glass.json",
        ".env",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_KNOWN_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


def _has_magic(value: str) -> bool:
    return any(char in value for char in _GLOB_MAGIC)


def validate_relative_pattern(pattern: str) -> str:
    """Return a canonical safe POSIX glob relative to an instance root."""

    if not isinstance(pattern, str):
        raise DataPolicyError("Protected path patterns must be strings.")
    value = pattern.strip()
    if not value or "\x00" in value:
        raise DataPolicyError("Protected path patterns must not be empty.")
    if "\\" in value:
        raise DataPolicyError(
            f"Protected path pattern must use POSIX separators: {pattern!r}"
        )
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise DataPolicyError(f"Protected path pattern must be relative: {pattern!r}")
    windows = PureWindowsPath(value)
    if windows.drive:
        raise DataPolicyError(
            f"Protected path pattern must not contain a drive: {pattern!r}"
        )
    while value.startswith("./"):
        value = value[2:]
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise DataPolicyError(f"Protected path pattern contains traversal: {pattern!r}")
    canonical = PurePosixPath(*parts).as_posix()
    if canonical == ".." or canonical.startswith("../"):
        raise DataPolicyError(f"Protected path pattern contains traversal: {pattern!r}")
    return canonical


def validate_relative_path(path: str) -> str:
    """Validate a concrete relative manifest path."""

    value = validate_relative_pattern(path)
    if any(_has_magic(part) or part == "**" for part in PurePosixPath(value).parts):
        raise DataPolicyError(f"Snapshot paths cannot contain globs: {path!r}")
    return value


def _is_forbidden_relative(relative_path: str) -> bool:
    lowered = relative_path.casefold()
    for prefix in _FORBIDDEN_PREFIXES:
        folded = prefix.casefold()
        if lowered == folded or lowered.startswith(folded + "/"):
            return True
    name = PurePosixPath(relative_path).name.casefold()
    if name in _KNOWN_SECRET_NAMES or name.startswith(".env."):
        return True
    return name.endswith(_KNOWN_SECRET_SUFFIXES)


def _ensure_entry_type(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(metadata.st_mode):
        raise UnsafePathError(f"Refusing symbolic link in protected data: {path}")
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise UnsafePathError(f"Refusing special file in protected data: {path}")
    return metadata


def _sorted_children(directory: Path) -> list[Path]:
    return sorted(
        (Path(entry.path) for entry in os.scandir(directory)),
        key=lambda item: item.name,
    )


def _safe_tree(path: Path) -> Iterator[Path]:
    """Yield a directory tree without ever following links."""

    metadata = _ensure_entry_type(path)
    yield path
    if stat.S_ISDIR(metadata.st_mode):
        for child in _sorted_children(path):
            yield from _safe_tree(child)


def _expand_parts(current: Path, parts: Sequence[str], index: int) -> Iterator[Path]:
    if index == len(parts):
        if current.exists() or current.is_symlink():
            yield current
        return

    segment = parts[index]
    if segment == "**":
        if index == len(parts) - 1:
            if current.exists() or current.is_symlink():
                yield from _safe_tree(current)
            return
        # ``**`` may consume no component.
        yield from _expand_parts(current, parts, index + 1)
        if not current.exists():
            return
        metadata = _ensure_entry_type(current)
        if not stat.S_ISDIR(metadata.st_mode):
            return
        for child in _sorted_children(current):
            child_metadata = _ensure_entry_type(child)
            if stat.S_ISDIR(child_metadata.st_mode):
                yield from _expand_parts(child, parts, index)
        return

    if not current.exists():
        return
    metadata = _ensure_entry_type(current)
    if not stat.S_ISDIR(metadata.st_mode):
        return

    if _has_magic(segment):
        candidates = (
            child
            for child in _sorted_children(current)
            if fnmatch.fnmatchcase(child.name, segment)
        )
    else:
        candidates = (current / segment,)

    for child in candidates:
        if not (child.exists() or child.is_symlink()):
            continue
        child_metadata = _ensure_entry_type(child)
        if index == len(parts) - 1:
            yield child
        elif stat.S_ISDIR(child_metadata.st_mode):
            yield from _expand_parts(child, parts, index + 1)


def expand_pattern(root: Path, pattern: str) -> tuple[Path, ...]:
    """Expand one confined pattern to regular files and directories."""

    root = Path(root).resolve(strict=True)
    canonical = validate_relative_pattern(pattern)
    matches: dict[str, Path] = {}
    for candidate in _expand_parts(root, PurePosixPath(canonical).parts, 0):
        # lstat validation above rejects symlinks before resolve can follow one.
        _ensure_entry_type(candidate)
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as exc:
            raise UnsafePathError(
                f"Protected path escaped the Silicon root: {candidate}"
            ) from exc
        if _is_forbidden_relative(relative):
            raise UnsafePathError(
                f"Protected data includes a credential or internal store: {relative}"
            )
        matches[relative] = candidate
    return tuple(matches[key] for key in sorted(matches))


def expand_pattern_files(root: Path, pattern: str) -> tuple[Path, ...]:
    """Expand one pattern and recursively include matched directories."""

    root = Path(root).resolve(strict=True)
    matches: dict[str, Path] = {}
    for candidate in expand_pattern(root, pattern):
        for item in _safe_tree(candidate):
            metadata = _ensure_entry_type(item)
            if not stat.S_ISREG(metadata.st_mode):
                continue
            relative = item.relative_to(root).as_posix()
            if _is_forbidden_relative(relative):
                raise UnsafePathError(
                    f"Protected data includes a credential or internal store: {relative}"
                )
            matches[relative] = item
    return tuple(matches[key] for key in sorted(matches))


def read_legacy_additions(root: Path) -> tuple[str, ...]:
    """Read safe additive entries from a legacy ``.backupsilicon`` file."""

    path = Path(root).resolve(strict=True) / LEGACY_POLICY_FILE
    if not path.exists() and not path.is_symlink():
        return ()
    metadata = _ensure_entry_type(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise DataPolicyError(
            f"{LEGACY_POLICY_FILE} must be a regular file before policy loading."
        )
    additions: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        value = raw_line.strip()
        if value and not value.startswith("#"):
            additions.append(validate_relative_pattern(value))
    return tuple(dict.fromkeys(additions))


def _read_local_additions(root: Path) -> Mapping[str, tuple[str, ...]]:
    path = root / POLICY_FILE
    if not path.exists() and not path.is_symlink():
        return {}
    metadata = _ensure_entry_type(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise DataPolicyError(f"{POLICY_FILE.as_posix()} must be a regular file.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DataPolicyError(f"Invalid {POLICY_FILE.as_posix()}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != POLICY_SCHEMA:
        raise DataPolicyError(
            f"{POLICY_FILE.as_posix()} must use policy schema {POLICY_SCHEMA}."
        )
    extra_keys = set(raw) - {"schema", "additive"}
    if extra_keys:
        raise DataPolicyError(
            "The local data policy may only contain schema and additive entries."
        )
    additive = raw.get("additive", {})
    if not isinstance(additive, dict):
        raise DataPolicyError("The data-policy additive field must be an object.")
    result: dict[str, tuple[str, ...]] = {}
    for class_name, patterns in additive.items():
        ownership = _CLASS_BY_NAME.get(str(class_name))
        if ownership is None or not ownership.snapshot:
            raise DataPolicyError(f"Unknown or non-snapshot data class: {class_name!r}")
        if not isinstance(patterns, list) or not all(
            isinstance(pattern, str) for pattern in patterns
        ):
            raise DataPolicyError(
                f"Additive class {class_name!r} must contain a list of paths."
            )
        result[ownership.name] = tuple(
            dict.fromkeys(validate_relative_pattern(item) for item in patterns)
        )
    return MappingProxyType(result)


@dataclass(frozen=True)
class DataPolicy:
    """Resolved mandatory policy plus local and legacy additions."""

    classes: Mapping[str, tuple[str, ...]]
    additive: Mapping[str, tuple[str, ...]]

    @property
    def snapshot_patterns(self) -> tuple[tuple[str, str], ...]:
        pairs: list[tuple[str, str]] = []
        for ownership in MANDATORY_CLASSES:
            if not ownership.snapshot:
                continue
            for pattern in self.classes.get(ownership.name, ()):
                pairs.append((ownership.name, pattern))
        for pattern in self.additive.get("legacy_additive", ()):
            pairs.append(("legacy_additive", pattern))
        return tuple(pairs)

    def resolve(self, root: Path) -> tuple[ProtectedFile, ...]:
        """Resolve the policy to a sorted, de-duplicated regular-file set."""

        root = Path(root).resolve(strict=True)
        selected: dict[str, tuple[Path, set[str]]] = {}
        for class_name, pattern in self.snapshot_patterns:
            for path in expand_pattern_files(root, pattern):
                relative = path.relative_to(root).as_posix()
                existing = selected.setdefault(relative, (path, set()))
                existing[1].add(class_name)
        return tuple(
            ProtectedFile(
                path=selected[key][0],
                relative_path=key,
                classes=tuple(sorted(selected[key][1])),
            )
            for key in sorted(selected)
        )

    def as_dict(self) -> dict:
        return {
            "schema": POLICY_SCHEMA,
            "mandatory": {
                item.name: list(item.patterns)
                for item in MANDATORY_CLASSES
                if item.snapshot
            },
            "additive": {
                key: list(value)
                for key, value in sorted(self.additive.items())
                if value
            },
            "excluded_credentials": list(_CLASS_BY_NAME["credentials"].patterns),
        }


def load_data_policy(
    root: Path,
    *,
    legacy_patterns: Iterable[str] | None = None,
) -> DataPolicy:
    """Load the immutable defaults and all permitted additive entries."""

    root = Path(root).resolve(strict=True)
    local = _read_local_additions(root)
    classes: dict[str, tuple[str, ...]] = {}
    for ownership in MANDATORY_CLASSES:
        if not ownership.snapshot:
            continue
        classes[ownership.name] = tuple(
            dict.fromkeys((*ownership.patterns, *local.get(ownership.name, ())))
        )
    if legacy_patterns is None:
        legacy = read_legacy_additions(root)
    else:
        legacy = tuple(
            dict.fromkeys(validate_relative_pattern(item) for item in legacy_patterns)
        )
    mandatory_patterns = {
        pattern for patterns in classes.values() for pattern in patterns
    }
    legacy = tuple(pattern for pattern in legacy if pattern not in mandatory_patterns)
    additive: dict[str, tuple[str, ...]] = {
        key: value for key, value in local.items() if value
    }
    if legacy:
        additive["legacy_additive"] = legacy
    return DataPolicy(
        classes=MappingProxyType(classes),
        additive=MappingProxyType(additive),
    )


def is_known_secret(path: str) -> bool:
    """Return whether a concrete relative path is plaintext-secret material."""

    return _is_forbidden_relative(validate_relative_path(path))
