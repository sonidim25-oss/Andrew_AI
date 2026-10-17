import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load():
    path = Path(__file__).resolve().parents[1] / 'skill_assets.py'
    spec = importlib.util.spec_from_file_location('skill_asset_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SkillAssetTests(unittest.TestCase):
    def fixture(self, root):
        names = ('voice-communication', 'image-creation', 'research-workflow')
        hashes = {}
        for name in names:
            path = root / 'source/skills' / name / 'SKILL.md'
            path.parent.mkdir(parents=True)
            path.write_text('# ' + name + '\n')
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        package = root / 'source/prototype/andrewbot'
        package.mkdir(parents=True)
        (package / 'skills.py').write_text('SKILL_DOCUMENT_HASHES = ' + repr(hashes) + '\n')
        target = root / 'installed/skills'
        return root / 'source', target

    def test_plans_exact_documents_from_versioned_loader_hashes(self):
        module = load()
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.fixture(Path(directory))
            plan = module.plan_skill_assets(source, target)
            self.assertEqual(3, len(plan))
            self.assertTrue(all(item['original'] is None for item in plan))
            self.assertEqual({source / 'skills' / item['name'] / 'SKILL.md' for item in plan},
                             {item['source'] for item in plan})
            self.assertTrue(all(item['path'] == target / item['name'] / 'SKILL.md' for item in plan))

    def test_refuses_modified_or_symlinked_source_and_unknown_installed_edits(self):
        module = load()
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.fixture(Path(directory))
            document = source / 'skills/voice-communication/SKILL.md'
            original = document.read_bytes()
            document.write_text('changed without updating tested loader hash')
            with self.assertRaises(ValueError):
                module.plan_skill_assets(source, target)
            document.write_bytes(original)
            installed = target / 'voice-communication/SKILL.md'
            installed.parent.mkdir(parents=True)
            installed.write_text('preserve this unrecognized local change')
            with self.assertRaises(ValueError):
                module.plan_skill_assets(source, target)
            installed.unlink()
            document.unlink()
            document.symlink_to(source / 'skills/image-creation/SKILL.md')
            with self.assertRaises(ValueError):
                module.plan_skill_assets(source, target)

    def test_accepts_document_hashes_from_previous_installed_loader_for_upgrade(self):
        module = load()
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.fixture(Path(directory))
            previous = {}
            for name in ('voice-communication', 'image-creation', 'research-workflow'):
                path = target / name / 'SKILL.md'
                path.parent.mkdir(parents=True)
                path.write_text('# previous ' + name)
                previous[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            package = target.parent / 'prototype/andrewbot'
            package.mkdir(parents=True)
            (package / 'skills.py').write_text('SKILL_DOCUMENT_HASHES = ' + repr(previous))
            plan = module.plan_skill_assets(source, target)
            self.assertTrue(all(item['original'] is not None for item in plan))

    def test_partial_install_restores_previous_and_removes_new_documents(self):
        module = load()
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.fixture(Path(directory))
            previous = {}
            for name in module.SKILL_NAMES:
                data = ('# previous ' + name).encode()
                previous[name] = hashlib.sha256(data).hexdigest()
                if name != 'research-workflow':
                    path = target / name / 'SKILL.md'
                    path.parent.mkdir(parents=True)
                    path.write_bytes(data)
            package = target.parent / 'prototype/andrewbot'
            package.mkdir(parents=True)
            (package / 'skills.py').write_text('SKILL_DOCUMENT_HASHES = ' + repr(previous))
            plan = module.plan_skill_assets(source, target)
            calls = []

            def interrupted_replace(path, data, gid):
                calls.append(path)
                path.write_bytes(data if len(calls) != 2 else b'partial write')
                if len(calls) == 2:
                    raise OSError('simulated interrupted document write')

            with patch.object(module.os, 'chown'):
                with self.assertRaises(OSError):
                    module.install_skill_assets(plan, 0, interrupted_replace)
            module.restore_skill_assets(plan, 0, lambda path, data, gid: path.write_bytes(data))
            for item in plan:
                if item['original'] is None:
                    self.assertFalse(item['path'].exists())
                else:
                    self.assertEqual(item['path'].read_bytes(), item['original'])
