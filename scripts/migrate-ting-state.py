#!/usr/bin/env python3
"""Move a stopped interpreter's Ting namespace using one IAM-verified identity mapping."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


def inventory(directory):
    entries = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Ting state must not contain symlinks: {path}')
        if path.is_file():
            entries[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise ValueError(f'Ting state must contain only regular files and directories: {path}')
    return entries


def migrate(home, mapping, backup=None, apply=False, stopped=False):
    if not isinstance(mapping, dict) or set(mapping) != {'old_id', 'new_id', 'org_id'}:
        raise ValueError(f'mapping must contain exactly old_id, new_id, and org_id, got {mapping!r}')
    old, new, org = (mapping[key] for key in ('old_id', 'new_id', 'org_id'))
    if not all(isinstance(value, str) for value in (old, new, org)):
        raise ValueError(f'mapping identifiers must be strings, got {mapping!r}')
    if not re.fullmatch(r'[a-z0-9_-]+:[a-z0-9_-]{3,50}', old):
        raise ValueError(f'old_id must be the complete legacy Silicon ID, got {old!r}')
    if not re.fullmatch(r'si:[a-z0-9_-]{3,50}', new):
        raise ValueError(f'new_id must be a canonical Silicon ID, got {new!r}')
    if old.rsplit(':', 1)[1] != org:
        raise ValueError(f'mapping owner {org!r} does not match the legacy owning organization of {old!r}')
    home = Path(home).resolve(strict=True)
    directory = home / '.silicon' / 'ting'
    for path in (home / '.silicon', directory):
        if path.is_symlink():
            raise ValueError(f'Ting state directories must not be symlinks: {path}')
    source, target = directory / old, directory / new
    if source.is_symlink() or target.is_symlink():
        raise ValueError(f'Ting identity directories must not be symlinks: {source} or {target}')
    if not source.is_dir():
        raise ValueError(f'legacy Ting namespace {source} is missing; inspect before retrying')
    if source != target and target.exists():
        raise ValueError(f'destination {target} already exists; never merge independent inboxes')
    files = inventory(source)
    receipt = dict(mapping=mapping, files=files, source=str(source), destination=str(target), applied=False)
    if not apply:
        return receipt
    if not stopped or backup is None:
        raise ValueError('apply requires a stopped interpreter and a new backup directory')
    backup = Path(backup).resolve()
    if backup == directory or directory in backup.parents:
        raise ValueError(f'backup {backup} must be outside the live Ting namespace {directory}')
    backup.mkdir(mode=0o700, parents=False, exist_ok=False)
    copied = backup / 'original'
    shutil.copytree(source, copied)
    for path in [copied, *copied.rglob('*')]:
        path.chmod(0o700 if path.is_dir() else 0o600)
        if path.is_file():
            with path.open('rb') as file:
                os.fsync(file.fileno())
    if inventory(copied) != files or inventory(source) != files:
        raise ValueError(f'backup verification failed or source changed ({copied} against {source}); no move performed')
    receipt_path = backup / 'receipt.json'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    receipt_path.chmod(0o600)
    with receipt_path.open('rb') as file:
        os.fsync(file.fileno())
    if source != target:
        # No writers are permitted. Rename preserves every inbox/hook byte together.
        source.rename(target)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if inventory(target) != files:
        raise ValueError(f'destination verification failed for {target}; keep interpreter stopped')
    receipt['applied'] = True
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--mapping', required=True, type=Path)
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--stopped', action='store_true', help='confirm all writers using this home are stopped')
    args = parser.parse_args()
    try:
        try:
            mapping = json.loads(args.mapping.read_text())
        except json.JSONDecodeError as error:
            raise ValueError(f'{args.mapping} is not valid JSON: {error}') from error
        result = migrate(args.home, mapping, args.backup_dir, args.apply, args.stopped)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'migration stopped: {error!r}\n' if isinstance(error, KeyError) else f'migration stopped: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
