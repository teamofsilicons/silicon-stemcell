#!/usr/bin/env python3
"""Offline interpreter state migration; preview by default. Use an approved IAM map.

Stop every interpreter using this home, and set --interpreter-home if customized.
Migrate Honeycomb's installed registry separately with Honeycomb's mapping tool.
This does not edit YAML, tokens, session files, or accepted Ting payloads.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_json(path):
    """JSON from a file; a parse failure names the file and the parser's position."""
    try:
        return json.loads(path.read_bytes())
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from error


def ordinary(path):
    """Never follow links or copy special files in migrated state."""
    require(not path.is_symlink(), f"state must not contain symlinks: {path}")
    if path.exists():
        require(path.is_file() or path.is_dir(), f"not a regular file/directory: {path}")
        if path.is_dir():
            for child in path.iterdir():
                ordinary(child)


def inventory(path):
    """Hash exact bytes and directory entries before backing up or moving state."""
    ordinary(path)
    if not path.exists():
        return None
    return {str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
            if item.is_file() else None for item in [path, *sorted(path.rglob("*"))]}


def sync(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def mappings(document, world):
    result = []
    for field, target in [("applications", "app_id"), ("identities", "public_id")]:
        rows = document.get(field, [])
        require(isinstance(rows, list), f"{field} must be an array, got {type(rows).__name__}: {rows!r}")
        mapping, destinations = {}, set()
        for row in rows:
            require(isinstance(row, dict), f"invalid {field} mapping {row!r}: expected an object")
            if row.get("testing_environment_id") != world:
                continue
            old, new = row.get("legacy_id"), row.get(target)
            require(isinstance(old, str) and isinstance(new, str),
                    f"invalid {field} IDs in {row!r}: legacy_id and {target} must be strings")
            require(old not in mapping, f"colliding {field} mapping: {old!r} maps to both {mapping.get(old)!r} and {new!r}")
            require(new not in destinations, f"colliding {field} mapping: {new!r} is the destination of more than one legacy ID, including {old!r}")
            if field == "applications":
                require(re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", new), f"invalid canonical app ID {new!r} for {old!r}")
                require(isinstance(row.get("org_id"), str) and re.fullmatch(r"[a-z0-9_-]{3,50}", row["org_id"])
                        and old.startswith(row["org_id"] + ">")
                        and old.count(">") == 1,
                        f"app mapping {old!r} -> {new!r} must retain verified ownership, but its org_id is {row.get('org_id')!r}")
            elif new.startswith("si:"):
                require(row.get("kind", "silicon") == "silicon" and row.get("actor_type", "silicon") == "silicon",
                        f"Silicon mapping {old!r} -> {new!r} has the wrong actor kind: kind {row.get('kind')!r}, actor_type {row.get('actor_type')!r}")
                if "org_id" in row:
                    require(isinstance(row["org_id"], str) and re.fullmatch(r"[a-z0-9_-]{3,50}", row["org_id"])
                            and old.rpartition(":")[2] == row["org_id"],
                            f"Silicon mapping {old!r} -> {new!r} disagrees with verified ownership: org_id {row['org_id']!r}")
            mapping[old] = new
            destinations.add(new)
        result.append(mapping)
    return result


def migrate(args):
    home = args.home.resolve(strict=True)
    state = home / ".silicon"
    require(state.is_dir() and not state.is_symlink(), f"home must have an ordinary .silicon directory: {state}")
    require(re.fullmatch(r"[a-z0-9_-]+:[a-z0-9_-]{3,50}", args.old_id)
            and not args.old_id.startswith(("si:", "c:")), f"--old-id must be the exact legacy Silicon ID, got {args.old_id!r}")
    require(re.fullmatch(r"[a-z0-9_-]{3,50}", args.org_id), f"invalid selected --org-id {args.org_id!r}")
    document = load_json(args.map)
    require(isinstance(document, dict), f"IAM mapping {args.map} must be an object, got {type(document).__name__}")
    apps, actors = mappings(document, args.testing_environment_id)
    new_id = actors.get(args.old_id, "")
    require(re.fullmatch(r"si:[a-z0-9_-]{3,50}", new_id),
            f"legacy Silicon {args.old_id!r} has no canonical mapping in IAM world {args.testing_environment_id!r}"
            + (f" (it maps to {new_id!r})" if new_id else ""))
    org_file = state / "org.json"
    ordinary(org_file)
    if org_file.exists():
        saved = load_json(org_file)
        require(saved == args.org_id, f"--org-id {args.org_id!r} must match the saved selected organization {saved!r} in {org_file}")

    ting = state / "ting"
    ordinary(ting)
    if ting.exists():
        for actor in ting.iterdir():
            require(actor.is_dir(), f"unexpected file in Ting identity directory: {actor}")
            require((re.fullmatch(r"si:[a-z0-9_-]{3,50}", actor.name) and actor.name in actors.values())
                    or re.fullmatch(r"si:[a-z0-9_-]{3,50}", actors.get(actor.name, "")),
                    f"Ting state contains an unmapped Silicon actor: {actor}")
            require(actor.name in (args.old_id, new_id),
                    f"Ting state belongs to another Silicon ({actor}); reconcile the home before migration")
    source, destination = ting / args.old_id, ting / new_id
    require(not source.exists() or not destination.exists(),
            f"canonical Ting directory {destination} already exists beside {source}; reconcile collision first")

    files = [state / name for name in ["auth-apps.json", "auth-checked.json", "auth-grants.json"]]
    for path in files:
        ordinary(path)
        require(not path.exists() or path.is_file(), f"expected a file: {path}")
    originals = {path: inventory(path) for path in [source, destination, org_file, *files]}
    registry = files[0]
    rewritten = None
    if registry.exists():
        commands = load_json(registry)
        require(isinstance(commands, list) and all(isinstance(c, str) for c in commands),
                f"invalid {registry}: expected a list of strings, got {commands!r}")
        for command in args.command:
            require(re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", command)
                    and (command in commands or "! " + command in commands),
                    f"--command {command!r} must name an exact saved bare executable (or its migrated ! entry); saved: {commands!r}")
        updated = []
        for command in commands:
            if re.fullmatch(r"[A-Za-z0-9_-]+>[A-Za-z0-9_-]+", command):
                require(command in apps, f"auth-apps.json contains an unmapped legacy app {command!r}")
                command = apps[command]
            elif command in args.command and "! " + command not in commands:
                command = "! " + command
            elif re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", command):
                require(command in apps.values(),
                        f"ambiguous bare auth-apps entry {command!r}; preserve a verified executable with --command {command}")
            require(command not in updated, f"auth-apps.json has colliding app/command entries for {command!r}")
            updated.append(command)
        if commands != updated:
            rewritten = (json.dumps(updated, indent=2) + "\n").encode()
    else:
        require(not args.command, "--command requires an existing auth-apps.json")
    changes = [p for p in files[1:] if p.exists()]
    if rewritten is not None:
        changes.insert(0, registry)
    if source.exists():
        changes.append(source)
    print(f"{'Apply' if args.apply else 'Preview'}: {args.old_id} -> {new_id}; {len(changes)} state paths")
    if not args.apply or not changes:
        return
    require(args.backup_dir is not None, "--apply requires --backup-dir")
    backup_root = args.backup_dir.resolve()
    require(backup_root != state and state not in backup_root.parents,
            f"backup directory {backup_root} must be outside {state}")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix="identifier-migration-", dir=backup_root))
    for path in changes:
        saved = backup / path.relative_to(home)
        saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_dir():
            shutil.copytree(path, saved)
        else:
            shutil.copy2(path, saved)
        require(inventory(saved) == originals[path],
                f"backup verification failed: {saved} does not match {path}; no live state changed")
    for path in [*sorted(backup.rglob("*"), reverse=True), backup]:
        path.chmod(0o700 if path.is_dir() else 0o600)
        sync(path)
    (backup / "migration.json").write_text(json.dumps({
        "home": str(home), "old_id": args.old_id, "new_id": new_id,
        "selected_org_id": args.org_id, "testing_environment_id": args.testing_environment_id,
        "files": {str(path.relative_to(home)): originals[path] for path in changes},
    }, indent=2) + "\n")
    (backup / "migration.json").chmod(0o600)
    sync(backup / "migration.json")
    sync(backup)
    sync(backup_root)
    changed = [str(path) for path, snapshot in originals.items() if inventory(path) != snapshot]
    require(not changed, f"source changed during backup: {', '.join(changed)}; no live state changed")
    moved, temporary = False, None
    try:
        if rewritten is not None:
            fd, temporary = tempfile.mkstemp(prefix=".auth-apps-", dir=state)
            with os.fdopen(fd, "wb") as stream:
                stream.write(rewritten)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, registry)
            temporary = None
        for cache in files[1:]:
            cache.unlink(missing_ok=True)
        if source.exists():
            source.rename(destination)
            moved = True
            require(inventory(destination) == originals[source],
                    f"destination verification failed for {destination}; keep the interpreter stopped")
            sync(ting)
        sync(state)
    except BaseException as error:
        # A failed rollback must not replace the failure that caused it.
        try:
            if moved:
                destination.rename(source)
            for path in changes:
                if path.is_file() or path in files:
                    shutil.copy2(backup / path.relative_to(home), path)
        except BaseException as rollback:
            raise ValueError(f"{error!r}; rolling back also failed: {rollback!r}; restore from {backup}") from error
        raise
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    print(f"Backed up changed state to {backup}; reconnect only after all coordinated migrations complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--map", required=True, type=Path)
    parser.add_argument("--old-id", required=True)
    parser.add_argument("--org-id", required=True, help="selected organization, not inferred ownership")
    parser.add_argument("--testing-environment-id", help="exact IAM test world; default is production")
    parser.add_argument("--command", action="append", default=[], help="exact old bare executable to preserve as ! NAME; repeatable")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--interpreter-home", type=Path, default=Path(os.environ.get(
        "SILICON_INTERPRETER_HOME", str(Path.home() / ".silicon-interpreter"))))
    args = parser.parse_args()
    try:
        require(not args.apply or args.backup_dir is not None, "--apply requires --backup-dir")
        args.interpreter_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.interpreter_home / "daemon.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            require(stat.S_ISREG(os.fstat(lock.fileno()).st_mode), "daemon lock must be a regular file")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("stop the interpreter before migrating its state") from None
            migrate(args)
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f"Migration refused: {error}\n")


if __name__ == "__main__":
    main()
