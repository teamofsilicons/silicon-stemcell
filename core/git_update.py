"""Git-based, pull-only self-update for a Silicon.

A Silicon evolves its own code, so an update is a **merge**, never an overwrite.
The goal is to **apply all of the upcoming version's new changes while keeping the
Silicon's own changes** — an integration, not a takeover. The Silicon's local
changes are committed, upstream is fetched, and the two are merged: a clean merge
keeps both the local edits and the new features; a genuine same-line conflict is
integrated by a resolver (codex→claude) that applies the upstream change *and*
preserves the Silicon's customisation — it does not just take one side.

The Silicon's living data and identity are never touched. The protected set is
the union of the **current and the incoming** ``.backupsilicon`` (so a path a
release *adds* — e.g. ``logs/**`` — is shielded by the very update that adds it)
plus ``silicon.json``. Those paths are git-ignored and snapshotted before the
merge, so they can never be overwritten or deleted.

PULL-ONLY: a Silicon can never push. The fetch URL is read-only, the push URL is
disabled, a refusing ``pre-push`` hook is installed, and nothing here ever runs
``git push``. Local commits/merges stay on the box.

This module only does git work; it never restarts the process — the caller
(glass_agent) restarts the instance after a successful update.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REMOTE = "origin"
BRANCH = os.environ.get("SILICON_REPO_BRANCH", "main")
# Read-only fetch URL. Overridable (mirrors / tests) but defaults to the canonical
# public repo. There is never a push URL — see harden_pull_only().
FETCH_URL = os.environ.get(
    "SILICON_REPO_URL", "https://github.com/teamofsilicons/silicon-stemcell.git"
)
MANIFEST = ".backupsilicon"
DEFAULT_MANIFEST = (
    "prompts/MEMORY.md",
    "prompts/memory/**",
    "prompts/LORE.md",
    "prompts/CONTACTS.md",
    "core/interface_state/contacts.json",
    "logs/**",
)
MANIFEST_ARCHIVE_PREFIX = ".backupsilicon.archive"
# Per-install identity + secrets that must never be tracked, clobbered, or
# deleted by an update. .glass.json carries the silicon's auth key/id; .env its
# local secrets; silicon.json its identity (name/brain).
ALWAYS_PROTECTED = ("silicon.json", ".glass.json", ".env")
_GITIGNORE_BEGIN = "# >>> silicon-managed (auto-synced from .backupsilicon) >>>"
_GITIGNORE_END = "# <<< silicon-managed <<<"


def log(msg: str) -> None:
    print(f"[git-update] {msg}", flush=True)


def _git(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def is_git_repo() -> bool:
    return _git("rev-parse", "--is-inside-work-tree").returncode == 0


# --------------------------------------------------------------------------- #
# Protected set (the union-pre-merge rule)
# --------------------------------------------------------------------------- #
def _parse_manifest(text: str) -> list[str]:
    globs: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            globs.append(line)
    return globs


def _default_manifest_text() -> str:
    return "\n".join(DEFAULT_MANIFEST) + "\n"


def _git_blob(ref: str) -> str:
    res = _git("show", f"{ref}:{MANIFEST}")
    return res.stdout if res.returncode == 0 and res.stdout.strip() else ""


def _unique_manifest_archive_path() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = PROJECT_ROOT / f"{MANIFEST_ARCHIVE_PREFIX}.{stamp}"
    path = base
    i = 1
    while path.exists():
        i += 1
        path = PROJECT_ROOT / f"{MANIFEST_ARCHIVE_PREFIX}.{stamp}.{i}"
    return path


def ensure_manifest_file() -> list[str]:
    """.backupsilicon is a manifest file, never a backup directory.

    A few legacy installs used ``.backupsilicon/`` as an archive folder. That
    path now belongs to the protected-file manifest. Preserve any legacy folder
    by renaming it aside, then restore the manifest from HEAD/upstream or write
    the default manifest.
    """
    path = PROJECT_ROOT / MANIFEST
    archived: list[str] = []
    if path.exists() and not path.is_file():
        archive = _unique_manifest_archive_path()
        path.rename(archive)
        archived.append(archive.name)
        log(f"archived legacy {MANIFEST} directory to {archive.name}")

    if path.is_file():
        return archived

    # Prefer the manifest already tracked by this install, then the incoming
    # version, then the built-in default used by fresh stemcells.
    if is_git_repo() and _git("checkout", "--", MANIFEST).returncode == 0 and path.is_file():
        return archived

    text = ""
    if is_git_repo():
        text = _git_blob("HEAD") or _git_blob(f"{REMOTE}/{BRANCH}")
    if not text:
        text = _default_manifest_text()
    path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    return archived


def _local_manifest() -> list[str]:
    ensure_manifest_file()
    path = PROJECT_ROOT / MANIFEST
    return _parse_manifest(path.read_text(encoding="utf-8")) if path.is_file() else []


def _incoming_manifest() -> list[str]:
    # The .backupsilicon the version being pulled declares — read WITHOUT merging,
    # so paths a release adds are protected before the merge can touch them.
    res = _git("show", f"{REMOTE}/{BRANCH}:{MANIFEST}")
    return _parse_manifest(res.stdout) if res.returncode == 0 else []


def protected_globs() -> list[str]:
    ensure_manifest_file()
    seen: list[str] = []
    for g in [*_local_manifest(), *_incoming_manifest(), *ALWAYS_PROTECTED]:
        if g not in seen:
            seen.append(g)
    return seen


def _matches_protected(rel_path: str, globs: list[str]) -> bool:
    rel = rel_path.replace(os.sep, "/")
    for g in globs:
        g = g.rstrip("/")
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g + "/*") or rel == g:
            return True
        # support "dir/**" style
        base = g[:-3] if g.endswith("/**") else g
        if rel == base or rel.startswith(base + "/"):
            return True
    return False


def sync_gitignore(globs: list[str]) -> None:
    """Rewrite the managed block in .gitignore so every protected path is ignored."""
    gi = PROJECT_ROOT / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    # strip any prior managed block
    out, skipping = [], False
    for line in existing.splitlines():
        if line.strip() == _GITIGNORE_BEGIN:
            skipping = True
            continue
        if line.strip() == _GITIGNORE_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    block = [_GITIGNORE_BEGIN, *sorted(set(globs)), _GITIGNORE_END]
    text = "\n".join(out).rstrip("\n") + "\n\n" + "\n".join(block) + "\n"
    gi.write_text(text, encoding="utf-8")


def _untrack(globs: list[str]) -> None:
    """Stop tracking any protected path that git currently tracks (keeps the
    working file). Idempotent — a no-op once they're untracked."""
    tracked = _git("ls-files").stdout.splitlines()
    to_remove = [p for p in tracked if _matches_protected(p, globs)]
    if to_remove:
        _git("rm", "-r", "--cached", "--ignore-unmatch", *to_remove)


# --------------------------------------------------------------------------- #
# Snapshot / restore protected files around the merge (belt-and-suspenders)
# --------------------------------------------------------------------------- #
def _snapshot(globs: list[str]) -> str:
    tmp = tempfile.mkdtemp(prefix="silicon-protect-")
    for rel in _git("ls-files", "--others", "--cached").stdout.splitlines():
        if _matches_protected(rel, globs):
            src = PROJECT_ROOT / rel
            if src.exists():
                dst = Path(tmp) / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    # also walk the working tree for protected files git doesn't know about
    for root, _dirs, files in os.walk(PROJECT_ROOT):
        if ".git" in root:
            continue
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), PROJECT_ROOT)
            if _matches_protected(rel, globs):
                dst = Path(tmp) / rel
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(os.path.join(root, fn), dst)
    return tmp


def _restore(snapshot_dir: str) -> None:
    base = Path(snapshot_dir)
    if not base.exists():
        return
    for root, _dirs, files in os.walk(base):
        for fn in files:
            src = Path(root) / fn
            rel = src.relative_to(base)
            dst = PROJECT_ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    shutil.rmtree(snapshot_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Pull-only hardening
# --------------------------------------------------------------------------- #
def harden_pull_only() -> None:
    """Read-only fetch, disabled push, refusing pre-push hook. Idempotent."""
    if _git("remote", "get-url", REMOTE).returncode == 0:
        _git("remote", "set-url", REMOTE, FETCH_URL)
    else:
        _git("remote", "add", REMOTE, FETCH_URL)
    _git("remote", "set-url", "--push", REMOTE, "DISABLED")
    hook = PROJECT_ROOT / ".git" / "hooks" / "pre-push"
    try:
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'silicon: push is disabled (pull-only)' >&2\nexit 1\n")
        hook.chmod(0o755)
    except OSError:
        pass


TEMPLATES_DIR = PROJECT_ROOT / "templates"


def seed_living_files() -> list[str]:
    """Create any missing living file from its template — NEVER overwrites.

    Living/identity files (MEMORY.md, LORE.md, CONTACTS.md, the memory/ dirs, …)
    are git-ignored, so a fresh clone won't have them and a release that adds a
    new one can't ship it directly. Templates under ``templates/`` mirror the live
    paths; on boot we copy template → live only when the live path is absent, so
    fresh installs get defaults and existing silicons keep what they have.
    """
    seeded: list[str] = []
    if not TEMPLATES_DIR.exists():
        return seeded
    for root, _dirs, files in os.walk(TEMPLATES_DIR):
        for fn in files:
            src = Path(root) / fn
            rel = src.relative_to(TEMPLATES_DIR)
            dst = PROJECT_ROOT / rel
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                seeded.append(str(rel))
    return seeded


def ensure_git_connected() -> dict:
    """Maintain the pull-only GitHub connection at every boot — harden the remote
    and fetch. Does NOT merge/reset (the update flow does that explicitly).

    A tarball install (no .git) is left untouched: the one-time migration
    establishes correct shared history from the version baseline first.
    Auto-initialising here without that baseline would mis-merge."""
    if not is_git_repo():
        return {"connected": False, "needs_migration": True}
    harden_pull_only()
    fetched = _git("fetch", "--tags", REMOTE, BRANCH, timeout=120)
    ensure_manifest_file()
    return {"connected": True, "fetch_ok": fetched.returncode == 0}


# --------------------------------------------------------------------------- #
# Conflict resolver (rare — only when the Silicon self-modified a line a release
# also changed). codex first, then claude. Integrate: apply upstream's new
# changes while keeping the Silicon's own.
# --------------------------------------------------------------------------- #
def _resolver_prompt(conflicted: list[str], notes: str, globs: list[str]) -> str:
    return (
        "You are the update resolver for a Silicon. A merge with the upstream just "
        "hit conflicts; resolve them in place, then stop. You only edit files — you "
        "are not talking to anyone and you must never run `git push`.\n\n"
        "GOAL — integrate, do not take over:\n"
        "- Every new feature/change in the upcoming (upstream) version MUST end up "
        "applied. Do not drop any upstream change.\n"
        "- The Silicon's OWN changes must be KEPT. This is not 'prefer incoming' — "
        "you are combining both sides. Where the same lines changed on both sides, "
        "merge them so the upstream's new behaviour is present AND the Silicon's "
        "customisation is preserved. Never simply throw away the Silicon's edits.\n"
        "- NEVER touch the Silicon's living data / identity (leave exactly as-is): "
        + ", ".join(globs) + ".\n"
        "- Remove every conflict marker (<<<<<<<, =======, >>>>>>>). None may remain. "
        "Leave each file valid.\n\n"
        f"What the upcoming version changes (release notes):\n{notes or '(none)'}\n\n"
        "Conflicted files (already in the working tree with markers): "
        + ", ".join(conflicted)
        + "\nResolve them now, applying the new changes while keeping the Silicon's."
    )


def _merge_upstream(globs: list[str]) -> dict:
    """Merge origin/main into HEAD so the upcoming version's new changes apply
    while the Silicon's own changes are kept. Clean merges need no brain; genuine
    same-line conflicts are integrated by the resolver (codex→claude). An
    unresolvable merge is aborted — the tree is left exactly as it was."""
    notes = _git("log", "--no-merges", "--pretty=%s", f"HEAD..{REMOTE}/{BRANCH}").stdout.strip()
    merge = _git("merge", "--no-edit", f"{REMOTE}/{BRANCH}", timeout=180)
    if merge.returncode == 0:
        return {"ok": True, "mode": "clean"}
    conflicted = [f for f in _git("diff", "--name-only", "--diff-filter=U").stdout.splitlines() if f]
    log(f"merge conflicts in {len(conflicted)} file(s): {conflicted}")
    if not _run_resolver(conflicted, notes, globs) or _markers_remain(conflicted):
        _git("merge", "--abort")
        return {"ok": False, "detail": "conflict resolution failed; merge aborted"}
    _git("add", *conflicted)
    _git("commit", "--no-edit")
    return {"ok": True, "mode": "resolved", "resolved": conflicted}


def _run_resolver(conflicted: list[str], notes: str, globs: list[str]) -> bool:
    prompt = _resolver_prompt(conflicted, notes, globs)
    attempts = [
        ["codex", "exec", "--full-auto", prompt],
        ["claude", "-p", "--dangerously-skip-permissions", prompt],
    ]
    for cmd in attempts:
        try:
            log(f"resolving conflicts with: {cmd[0]}")
            # Bounded so a stuck/hung resolver can't stall a fleet rollout — a
            # real conflict resolve takes a minute or two; on timeout we fall
            # through to the next brain, then to a safe merge --abort.
            subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300)
        except Exception as exc:  # noqa: BLE001
            log(f"{cmd[0]} resolver failed/timeout: {exc}")
            continue
        if not _markers_remain(conflicted):
            return True
    return not _markers_remain(conflicted)


def _markers_remain(files: list[str]) -> bool:
    for rel in files:
        p = PROJECT_ROOT / rel
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "<<<<<<<" in text or ">>>>>>>" in text:
            return True
    return False


# --------------------------------------------------------------------------- #
# The update
# --------------------------------------------------------------------------- #
def _local_version() -> str:
    import json

    p = PROJECT_ROOT / "silicon.info"
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("version") or "")
    except Exception:
        return ""


def migrate(baseline: str = "") -> dict:
    """One-time, NON-DESTRUCTIVE conversion of a tarball install into a pull-only
    git checkout of main. Loses nothing and NEVER uses ``reset --hard``:

    - all protected data (.backupsilicon paths + .glass.json/.env/silicon.json)
      is snapshotted and restored around the whole operation;
    - HEAD is seeded at the install's version baseline with ``reset --mixed``
      (index only — the working tree is never touched);
    - the silicon's own code (tracked mods) is committed, then main is merged so
      the upcoming version's new changes apply while the silicon's own changes are
      kept — a stock install fast-forwards; a self-modified one keeps its edits and
      genuine same-line conflicts are integrated by the resolver (codex→claude);
    - untracked files (incl. secrets) are never staged or deleted;
    - any merge failure aborts the merge and leaves the install untouched.

    ``baseline`` is the upstream commit matching the install's current version
    (pass the v<version> commit) so the merge has a real common ancestor.
    """
    if is_git_repo():
        return {"status": "already_git", "version": _local_version()}
    _git("init")
    _git("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    harden_pull_only()
    if _git("fetch", "--tags", REMOTE, BRANCH, timeout=180).returncode != 0:
        return {"status": "error", "detail": "fetch failed"}
    base = baseline or f"{REMOTE}/{BRANCH}"
    if _git("rev-parse", "--verify", base).returncode != 0:
        base = f"{REMOTE}/{BRANCH}"

    globs = protected_globs()
    snapshot = _snapshot(globs)
    try:
        _git("reset", "--mixed", base)  # HEAD+index = baseline; working tree untouched
        sync_gitignore(globs)
        _untrack(globs)
        if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            _git("add", "-u")  # tracked code mods only — never untracked secrets
            _git("commit", "-m", "silicon: adopt local code at migration baseline")
        merged = _merge_upstream(globs)
        if not merged["ok"]:
            return {"status": "error", "detail": merged["detail"]}
    finally:
        _restore(snapshot)

    seed_living_files()
    return {"status": "migrated", "version": _local_version(), "mode": merged.get("mode")}


def git_apply() -> dict:
    """Pull-only merge update. Returns a result dict; never restarts (caller does)."""
    if not is_git_repo():
        return {"status": "error", "detail": "not a git repo; run migration first"}

    ensure_git_connected()
    if _git("rev-parse", f"{REMOTE}/{BRANCH}").returncode != 0:
        return {"status": "error", "detail": "could not fetch upstream"}

    # Protect against the UNION of current + incoming .backupsilicon, BEFORE merging.
    globs = protected_globs()
    sync_gitignore(globs)
    _untrack(globs)
    snapshot = _snapshot(globs)

    before = _local_version()
    try:
        # Snapshot the Silicon's own code changes so the merge preserves them.
        # `add -u` stages modifications to ALREADY-TRACKED files only — never
        # untracked per-install secrets/dotfiles (.glass.json, .env, …), which
        # must not be committed (else a later reset would delete them).
        if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            _git("add", "-u")
            _git("commit", "-m", f"silicon: local changes before update {int(time.time())}")

        # Already current?
        behind = _git("rev-list", "--count", f"HEAD..{REMOTE}/{BRANCH}").stdout.strip()
        if behind in ("", "0"):
            return {"status": "up_to_date", "version": before}

        merged = _merge_upstream(globs)
        if not merged["ok"]:
            return {"status": "error", "detail": merged["detail"], "version": before}
        return {"status": "updated", "version": _local_version(),
                "mode": merged["mode"], "resolved": merged.get("resolved", [])}
    finally:
        _restore(snapshot)
