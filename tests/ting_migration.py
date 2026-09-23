#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('migration', Path(__file__).resolve().parents[1] / 'scripts/migrate-ting-state.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class Migration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.old = self.home / '.silicon/ting/assistant:owner_org'
        self.new = self.old.with_name('si:renamed')
        (self.old / 'selected-org').mkdir(parents=True)
        (self.old / 'selected-org/inbox.json').write_bytes(b'{"seen":{"id":42},"pending":[{"type":"tos>dm.sync.changed","data":{"text":"untouched si:example"}}]}\n')
        (self.old / 'selected-org/hook.json').write_text('"stable-hook"\n')
        self.mapping = dict(old_id='assistant:owner_org', new_id='si:renamed', org_id='owner_org')

    def test_preview_then_atomic_move_preserves_every_byte(self):
        files = migration.inventory(self.old)
        preview = migration.migrate(self.home, self.mapping)
        self.assertFalse(preview['applied'])
        self.assertTrue(self.old.exists())
        result = migration.migrate(self.home, self.mapping, self.root / 'backup', True, True)
        self.assertTrue(result['applied'])
        self.assertFalse(self.old.exists())
        self.assertEqual(files, migration.inventory(self.new))
        self.assertEqual(files, migration.inventory(self.root / 'backup/original'))
        self.assertTrue(json.loads((self.root / 'backup/receipt.json').read_text())['applied'])

    def test_collision_or_wrong_owner_never_changes_source(self):
        self.new.mkdir()
        for mapping in [self.mapping, dict(self.mapping, org_id='another')]:
            with self.assertRaises(ValueError):
                migration.migrate(self.home, mapping, self.root / 'backup', True, True)
        self.assertTrue(self.old.exists())
        self.assertFalse((self.root / 'backup').exists())

    def test_apply_requires_stopped_and_verified_backup(self):
        for backup, stopped in [(None, True), (self.root / 'backup', False)]:
            with self.assertRaises(ValueError):
                migration.migrate(self.home, self.mapping, backup, True, stopped)
        self.assertTrue(self.old.exists())

    def test_symlink_is_rejected(self):
        (self.old / 'linked').symlink_to(self.root)
        with self.assertRaises(ValueError):
            migration.migrate(self.home, self.mapping)


if __name__ == '__main__':
    unittest.main()
