import importlib.util
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class ControllerPackageTests(unittest.TestCase):
    def load(self):
        path = ROOT / 'scripts/install-controller-package.py'
        self.assertTrue(path.is_file(), 'all deployment paths need one transactional package installer')
        spec = importlib.util.spec_from_file_location('controller_package_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fixture(self, root):
        source = root / 'source'
        shutil.copytree(ROOT / 'skills', source / 'skills')
        package = source / 'prototype/andrewbot'
        package.mkdir(parents=True)
        shutil.copy2(ROOT / 'prototype/andrewbot/skills.py', package / 'skills.py')
        (package / '__init__.py').write_text('')
        (package / 'controller.py').write_text('VERSION = 2\n')
        return source, root / 'installed/prototype/andrewbot'

    def test_fresh_package_can_load_every_exact_document(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.fixture(Path(directory))
            with patch.object(module.os, 'chown'), patch.object(module.os, 'fchown'):
                module.install_package(source, target, os.getgid())
            spec = importlib.util.spec_from_file_location('installed_skills', target / 'skills.py')
            loader = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loader)
            catalog = loader.SkillCatalog(target.parents[1] / 'skills')
            for name in loader.SKILL_DOCUMENT_HASHES:
                self.assertEqual(catalog.execute('skills.load', {'name': name})['instructions'],
                                 (source / 'skills' / name / 'SKILL.md').read_text())

    def test_interrupted_upgrade_restores_loader_modules_and_documents(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.fixture(Path(directory))
            with patch.object(module.os, 'chown'), patch.object(module.os, 'fchown'):
                module.install_package(source, target, os.getgid())
                before = {p: p.read_bytes() for p in target.parents[1].rglob('*') if p.is_file()}
                (source / 'prototype/andrewbot/controller.py').write_text('VERSION = 3\n')
                original = module.replace_file
                failed = [False]
                def interrupt(path, data, gid, **kwargs):
                    if path.name == 'SKILL.md' and not failed[0]:
                        failed[0] = True
                        raise OSError('fixture interrupted document installation')
                    return original(path, data, gid, **kwargs)
                with patch.object(module, 'replace_file', side_effect=interrupt):
                    with self.assertRaises(OSError):
                        module.install_package(source, target, os.getgid())
                self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_upgrade_installs_new_loader_and_document_and_durable_backup_restores_both(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.fixture(Path(directory))
            with patch.object(module.os, 'chown'), patch.object(module.os, 'fchown'):
                module.install_package(source, target, os.getgid())
                original = {p: p.read_bytes() for p in target.parents[1].rglob('*') if p.is_file()}
                document = source / 'skills/image-creation/SKILL.md'
                previous = hashlib.sha256(document.read_bytes()).hexdigest()
                document.write_text(document.read_text() + '\nUpdated guidance.\n')
                current = hashlib.sha256(document.read_bytes()).hexdigest()
                loader = source / 'prototype/andrewbot/skills.py'
                loader.write_text(loader.read_text().replace(previous, current))
                backup = Path(directory) / 'package.json'
                module.install_package(source, target, os.getgid(), backup)
                self.assertEqual((target / 'skills.py').read_bytes(), loader.read_bytes())
                self.assertEqual((target.parents[1] / 'skills/image-creation/SKILL.md').read_bytes(), document.read_bytes())
                module.restore_backup(backup)
                self.assertEqual(original, {p: p.read_bytes() for p in original})

    def test_unknown_local_document_refuses_before_replacing_modules(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.fixture(Path(directory))
            with patch.object(module.os, 'chown'), patch.object(module.os, 'fchown'):
                module.install_package(source, target, os.getgid())
                document = target.parents[1] / 'skills/image-creation/SKILL.md'
                document.write_text('unrecognized local edit')
                (source / 'prototype/andrewbot/controller.py').write_text('VERSION = 3\n')
                with self.assertRaises(ValueError):
                    module.install_package(source, target, os.getgid())
                self.assertEqual((target / 'controller.py').read_text(), 'VERSION = 2\n')

    def test_every_supported_entrypoint_installs_matching_assets_on_fresh_install_and_upgrade(self):
        for entrypoint in ('admin-setup.sh', 'activate-phase1.sh',
                           'validate-and-start-controller.sh', 'enable-browser-runtime.sh'):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, target = self.fixture(root)
                scripts = source / 'scripts'
                scripts.mkdir()
                for name in ('install-controller-package.py', 'skill_assets.py'):
                    shutil.copy2(ROOT / 'scripts' / name, scripts / name)
                commands = [line for line in (ROOT / 'scripts' / entrypoint).read_text().splitlines()
                            if 'install-controller-package.py"' in line and '--restore-backup' not in line]
                self.assertEqual(len(commands), 1)
                launcher = root / 'launch.py'
                launcher.write_text('''import os,grp,runpy,sys,types
from unittest.mock import patch
sys.argv=sys.argv[1:]
with patch.object(os,'geteuid',return_value=0), patch.object(os,'chown'), patch.object(os,'fchown'), patch.object(grp,'getgrnam',return_value=types.SimpleNamespace(gr_gid=os.getgid())):
    runpy.run_path(sys.argv[0],run_name='__main__')
''')
                command = commands[0].replace('/opt/andrew', str(root / 'installed'))
                package_target = target if entrypoint == 'validate-and-start-controller.sh' else target.parent
                for upgrade in (False, True):
                    backup = root / ('upgrade-backup' if upgrade else 'fresh-backup')
                    backup.mkdir()
                    if upgrade:
                        document = source / 'skills/image-creation/SKILL.md'
                        previous = hashlib.sha256(document.read_bytes()).hexdigest()
                        document.write_text(document.read_text() + '\nA new revision.\n')
                        current = hashlib.sha256(document.read_bytes()).hexdigest()
                        loader = source / 'prototype/andrewbot/skills.py'
                        loader.write_text(loader.read_text().replace(previous, current))
                    actual = command if '--backup' in command else command + ' --backup "$BACKUP/controller-assets.json"'
                    environment = dict(os.environ, PROJECT=str(source), PROJECT_DIR=str(source),
                                       INSTALL_DIR=str(target.parent), TARGET=str(package_target),
                                       BACKUP=str(backup), PACKAGE_LAUNCHER=str(launcher))
                    result = subprocess.run(['bash', '-euc',
                        'python3() { /usr/bin/python3 "$PACKAGE_LAUNCHER" "$@"; }\n' + actual],
                        env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((target / 'skills.py').read_bytes(),
                                     (source / 'prototype/andrewbot/skills.py').read_bytes())
                    for document in (source / 'skills').glob('*/SKILL.md'):
                        self.assertEqual((target.parents[1] / 'skills' / document.parent.name / 'SKILL.md').read_bytes(),
                                         document.read_bytes())

