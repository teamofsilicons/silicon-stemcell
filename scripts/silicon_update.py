#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


META_DIR_NAME = ".silicon-upstream"
BASE_DIR_NAME = "base"
META_FILE_NAME = "meta.json"
REPO_URL = "https://github.com/unlikefraction/silicon-stemcell.git"
MIN_INFERENCE_MATCHES = 3
MAX_INFERENCE_CANDIDATES = 12

IGNORE_EXACT = {
    ".DS_Store",
    ".glass.json",
    ".restart_pending",
    ".silicon.err.log",
    ".silicon.log",
    ".silicon.pid",
    "core/cron/checkbacks.json",
    "core/cron/history.json",
    "core/interface_state/contacts.json",
    "core/interface_state/contacts_backup.json",
    "core/interface_state/crons.json",
    "core/interface_state/manager_queue.json",
    "env.py",
    "worker/outputs/_active_workers.json",
    "worker/outputs/_archive_meta.json",
    "worker/outputs/_browser_queue.json",
    "worker/outputs/_worker_registry.json",
}

IGNORE_PREFIXES = {
    ".git/",
    ".silicon-upstream/",
    "__pycache__/",
    "core/interface_state/media/",
    "sessions/",
    "worker/outputs/",
}

# Files that are always replaced from upstream, but only when the entire
# update succeeds without conflicts.  This prevents the local version tag
# from advancing when the codebase is only partially updated.
REPLACE_ON_SUCCESS = {
    "silicon.info",
}

TEXT_EXTENSIONS = {
    ".json", ".md", ".py", ".ps1", ".sh", ".txt", ".toml", ".yaml", ".yml", ".gitignore"
}


def normalize_rel(path: Path) -> str:
    return path.as_posix()


def should_ignore(rel: str) -> bool:
    parts = rel.split("/")
    if "__pycache__" in parts or rel.endswith((".pyc", ".pyo")):
        return True
    if rel in IGNORE_EXACT:
        return True
    return any(rel.startswith(prefix) for prefix in IGNORE_PREFIXES)


def collect_files(root: Path) -> dict[str, Path]:
    files = {}
    if not root.exists():
        return files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = normalize_rel(path.relative_to(root))
        if should_ignore(rel):
            continue
        files[rel] = path
    return files


def read_bytes(path: Optional[Path]) -> Optional[bytes]:
    if not path or not path.exists():
        return None
    return path.read_bytes()


def is_text_path(path: Path, blob: Optional[bytes] = None) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if blob is None:
        blob = read_bytes(path)
    if blob is None:
        return False
    try:
        blob.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def write_file(path: Path, data: bytes, mode_source: Optional[Path] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mode_source and mode_source.exists():
        os.chmod(path, mode_source.stat().st_mode)


def replace_dir_contents(dst: Path, src: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for rel, src_path in collect_files(src).items():
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, target)


def save_meta(target: Path, source: Path, extra: Optional[dict] = None) -> None:
    meta_dir = target / META_DIR_NAME
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "source": str(source),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        meta.update(extra)
    (meta_dir / META_FILE_NAME).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def git_merge_file(local: bytes, base: bytes, upstream: bytes, rel: str) -> Tuple[bool, bytes]:
    with tempfile.TemporaryDirectory(prefix="silicon-merge-") as tmp:
        tmp_path = Path(tmp)
        local_path = tmp_path / "local"
        base_path = tmp_path / "base"
        upstream_path = tmp_path / "upstream"
        local_path.write_bytes(local)
        base_path.write_bytes(base)
        upstream_path.write_bytes(upstream)
        proc = subprocess.run(
            ["git", "merge-file", "-p", str(local_path), str(base_path), str(upstream_path)],
            capture_output=True,
        )
        if proc.returncode == 0:
            return True, proc.stdout
        return False, proc.stdout


def run_git(args: list[str], cwd: Path, input_bytes: Optional[bytes] = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_bytes,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="ignore").strip() or f"git {' '.join(args)} failed")
    return proc


def prepare_history_repo(source: Path) -> Tuple[Optional[Path], Optional[tempfile.TemporaryDirectory]]:
    if (source / ".git").exists():
        return source, None

    script_repo = Path(__file__).resolve().parent.parent
    if (script_repo / ".git").exists():
        return script_repo, None

    if shutil.which("git") is None:
        return None, None

    temp_dir = tempfile.TemporaryDirectory(prefix="silicon-history-")
    history_repo = Path(temp_dir.name) / "repo"
    proc = subprocess.run(
        ["git", "clone", "--quiet", REPO_URL, str(history_repo)],
        capture_output=True,
    )
    if proc.returncode != 0:
        temp_dir.cleanup()
        return None, None
    return history_repo, temp_dir


def score_commit(history_repo: Path, commit: str, local_files: dict[str, Path]) -> Tuple[int, int]:
    score = 0
    shared = 0
    for rel, local_path in local_files.items():
        proc = run_git(["show", f"{commit}:{rel}"], cwd=history_repo, check=False)
        if proc.returncode != 0:
            continue
        shared += 1
        if proc.stdout == local_path.read_bytes():
            score += 1
    return score, shared


def materialize_commit(history_repo: Path, commit: str, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination.parent / f"{commit}.tar"
    try:
        run_git(["archive", "-o", str(archive_path), commit], cwd=history_repo)
        shutil.unpack_archive(str(archive_path), str(destination), format="tar")
    finally:
        archive_path.unlink(missing_ok=True)


def infer_base_commit(source: Path, target: Path) -> Optional[tuple[str, int, int]]:
    history_repo, temp_dir = prepare_history_repo(source)
    if history_repo is None:
        return None

    try:
        local_files = collect_files(target)
        if not local_files:
            return None

        candidate_hits: dict[str, int] = {}

        for rel, local_path in sorted(local_files.items()):
            blob_hash = run_git(["hash-object", "--stdin"], cwd=history_repo, input_bytes=local_path.read_bytes()).stdout.decode("utf-8").strip()
            if not blob_hash:
                continue
            proc = run_git(
                ["log", "--all", "--format=%H", f"--find-object={blob_hash}", "--", rel],
                cwd=history_repo,
                check=False,
            )
            if proc.returncode != 0:
                continue
            commits = {line.strip() for line in proc.stdout.decode("utf-8", errors="ignore").splitlines() if line.strip()}
            for commit in commits:
                candidate_hits[commit] = candidate_hits.get(commit, 0) + 1

        if not candidate_hits:
            return None

        ranked = sorted(candidate_hits.items(), key=lambda item: (-item[1], item[0]))[:MAX_INFERENCE_CANDIDATES]
        best_commit = None
        best_score = -1
        best_shared = 0

        for commit, _ in ranked:
            score, shared = score_commit(history_repo, commit, local_files)
            if score > best_score or (score == best_score and shared > best_shared):
                best_commit = commit
                best_score = score
                best_shared = shared

        if best_commit is None or best_score < MIN_INFERENCE_MATCHES:
            return None

        return best_commit, best_score, best_shared
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def ensure_base_snapshot(source: Path, target: Path) -> bool:
    base_dir = target / META_DIR_NAME / BASE_DIR_NAME
    if collect_files(base_dir):
        return True

    inferred = infer_base_commit(source, target)
    if inferred is None:
        return False

    commit, score, shared = inferred
    history_repo, temp_dir = prepare_history_repo(source)
    if history_repo is None:
        return False

    try:
        materialize_commit(history_repo, commit, base_dir)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    save_meta(
        target,
        source,
        {
            "inferred_base_commit": commit,
            "inferred_base_matches": score,
            "inferred_base_shared": shared,
        },
    )
    print(f"Inferred legacy upstream base: {commit[:12]} ({score} exact file matches, {shared} shared files)")
    return True


def snapshot(source: Path, target: Path) -> int:
    base_dir = target / META_DIR_NAME / BASE_DIR_NAME
    replace_dir_contents(base_dir, source)
    save_meta(target, source)
    print(f"Snapshot refreshed from {source}")
    return 0


def update(source: Path, target: Path) -> int:
    if not ensure_base_snapshot(source, target):
        print("Update aborted. No upstream base snapshot exists, and the original upstream version could not be inferred safely.", file=sys.stderr)
        print("No files were changed.", file=sys.stderr)
        return 2

    upstream_files = collect_files(source)
    base_dir = target / META_DIR_NAME / BASE_DIR_NAME
    base_files = collect_files(base_dir)
    target_files = collect_files(target)

    writes: list[tuple[Path, bytes, Path]] = []
    base_writes: list[tuple[Path, bytes, Path]] = []
    added = 0
    updated = 0
    merged = 0
    kept_local = 0
    preserved_local_conflicts: list[str] = []

    git_available = shutil.which("git") is not None

    deferred_replace: list[tuple[Path, bytes, Path]] = []

    for rel, upstream_path in sorted(upstream_files.items()):
        local_path = target / rel
        base_path = base_dir / rel

        upstream_bytes = read_bytes(upstream_path)
        local_bytes = read_bytes(local_path)
        base_bytes = read_bytes(base_path)

        # Defer silicon.info-style files — replace only on full success
        if rel in REPLACE_ON_SUCCESS:
            deferred_replace.append((local_path, upstream_bytes, upstream_path))
            base_writes.append((base_path, upstream_bytes, upstream_path))
            continue

        if local_bytes is None:
            writes.append((local_path, upstream_bytes, upstream_path))
            base_writes.append((base_path, upstream_bytes, upstream_path))
            added += 1
            continue

        if local_bytes == upstream_bytes:
            base_writes.append((base_path, upstream_bytes, upstream_path))
            continue

        if base_bytes is None:
            preserved_local_conflicts.append(rel)
            continue

        if local_bytes == base_bytes:
            writes.append((local_path, upstream_bytes, upstream_path))
            base_writes.append((base_path, upstream_bytes, upstream_path))
            updated += 1
            continue

        if upstream_bytes == base_bytes:
            kept_local += 1
            base_writes.append((base_path, upstream_bytes, upstream_path))
            continue

        if git_available and is_text_path(local_path, local_bytes) and is_text_path(base_path, base_bytes) and is_text_path(upstream_path, upstream_bytes):
            ok, merged_bytes = git_merge_file(local_bytes, base_bytes, upstream_bytes, rel)
            if ok:
                writes.append((local_path, merged_bytes, upstream_path))
                base_writes.append((base_path, upstream_bytes, upstream_path))
                merged += 1
                continue

        preserved_local_conflicts.append(rel)

    for path, data, mode_source in writes:
        write_file(path, data, mode_source)

    for path, data, mode_source in base_writes:
        write_file(path, data, mode_source)

    # Only replace deferred files (silicon.info) if no conflicts
    if not preserved_local_conflicts:
        for path, data, mode_source in deferred_replace:
            write_file(path, data, mode_source)

    save_meta(target, source)

    print(f"Updated safely: {updated} replaced, {merged} auto-merged, {added} added, {kept_local} kept-local.")
    if preserved_local_conflicts:
        print("Preserved local versions for these files because a clean merge was not possible:")
        for rel in preserved_local_conflicts:
            print(f"  - {rel}")
    if not writes:
        print("Already up to date or all local changes were preserved without upstream edits.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("snapshot", "update"):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True)
        p.add_argument("--target", required=True)

    args = parser.parse_args()
    source = Path(args.source).resolve()
    target = Path(args.target).resolve()

    if not source.exists():
        print(f"Source not found: {source}", file=sys.stderr)
        return 1
    if not target.exists():
        print(f"Target not found: {target}", file=sys.stderr)
        return 1

    if args.cmd == "snapshot":
        return snapshot(source, target)
    return update(source, target)


if __name__ == "__main__":
    raise SystemExit(main())
