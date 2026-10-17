import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('autonomy_media_installer', ROOT / 'scripts/install-autonomy-media.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class AutonomyInstallerTests(unittest.TestCase):
    def test_merge_preserves_unrelated_live_changes(self):
        base = b'def owner():\n    return 120\n\ndef research():\n    return "old research"\n'
        live = base.replace(b'old research', b'live research checkpoint')
        proposed = base.replace(b'return 120', b'return None')
        with tempfile.TemporaryDirectory() as directory:
            merged = installer.merge_source(live, base, proposed, Path(directory), 'example.py')
        self.assertIn(b'return None', merged)
        self.assertIn(b'live research checkpoint', merged)

    def test_overlapping_live_changes_are_refused_before_installation(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'overlap'):
            installer.merge_source(b'x = 10\n', b'x = 1\n', b'x = 0\n', Path(directory), 'example.py')

    def test_guidance_is_persisted_once_with_owner_provenance_and_voice_on(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / 'controller.sqlite3')
            helper = Mock()
            def execute(*args):
                code = args[args.index('-c') + 1].replace('/var/lib/andrew/control/controller.sqlite3', database)
                return subprocess.check_output([sys.executable, '-c', code, installer.GUIDANCE],
                                               env={'PYTHONPATH': str(ROOT / 'prototype')}, text=True)
            helper.command.side_effect = execute
            with patch('builtins.print'):
                installer.record_owner_guidance(helper)
                installer.record_owner_guidance(helper)
            with sqlite3.connect(database) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM memory_records').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT source FROM memory_records').fetchone()[0], 'owner')
                state = dict(db.execute('SELECT key,value FROM state'))
                self.assertEqual(state['media_spontaneous_voice'], '1')
                self.assertEqual(state['owner_credit_guidance'], installer.GUIDANCE)

    def test_snapshot_contains_exact_proposed_code_and_matching_skill_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'installed/prototype/andrewbot'
            package.mkdir(parents=True)
            (package / 'appserver.py').write_bytes(b'# live appserver\n')
            (package / 'extra.py').write_bytes(b'# live extra module\n')
            skills = root / 'installed/skills'
            for name in installer.SKILLS:
                (skills / name).mkdir(parents=True)
                (skills / name / 'SKILL.md').write_text('Previous guidance')
            backup = root / 'backup'
            backup.mkdir()
            with patch.object(installer, 'TARGET', package):
                snapshot = installer.snapshot(backup, {'appserver.py': b'# proposed merged appserver\n'})
            self.assertEqual((snapshot / 'prototype/andrewbot/appserver.py').read_bytes(), b'# proposed merged appserver\n')
            self.assertEqual((snapshot / 'prototype/andrewbot/extra.py').read_bytes(), b'# live extra module\n')
            for name in installer.SKILLS:
                self.assertEqual((snapshot / 'skills' / name / 'SKILL.md').read_bytes(),
                                 (ROOT / 'skills' / name / 'SKILL.md').read_bytes())
            self.assertTrue((snapshot / 'prototype/tests/test_autonomous_media.py').is_file())
