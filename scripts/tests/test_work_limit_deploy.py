import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def load_assets(case):
    path = ROOT / 'scripts/work_limit_assets.py'
    case.assertTrue(path.exists(), 'reviewed image/config deployment helper is missing')
    spec = importlib.util.spec_from_file_location('work_limit_assets', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkLimitDeployTests(unittest.TestCase):
    def fixture(self, directory, helper):
        root = Path(directory)
        config = root / 'runner.conf'
        config.write_text('IMAGE=localhost/old@sha256:' + 'a' * 64 + '\nWORKSPACE=/fixture/projects\n')
        other = root / 'other-policy'
        other.write_bytes(b'unchanged policy')
        checks = root / 'installed-check.py'
        checks.write_bytes(b'# original check\n')
        (root / 'runtime').mkdir()
        sources = ('runtime/Containerfile.release', 'runtime/browser_entry.py', 'runtime/worker_entry.py',
                   'prototype/andrewbot/browser_protocol.py')
        for source in sources:
            path = root / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'# reviewed source\n')
        (root / 'scripts').mkdir()
        (root / 'scripts/verify-runtime.py').write_bytes(b'# new check\n')
        archive = root / 'image.tar'
        archive.write_bytes(b'fixed archive')
        release = {'image': 'localhost/new@sha256:' + 'b' * 64,
                   'image_config_id': 'sha256:' + 'c' * 64,
                   'archive_path': str(archive), 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                   'sources': {source: hashlib.sha256((root/source).read_bytes()).hexdigest() for source in sources}}
        (root / 'runtime/browser-release.json').write_text(json.dumps(release))
        readiness = types.SimpleNamespace(POLICY_FILES=[config, other], protected_read=lambda p: Path(p).read_bytes())
        digest = hashlib.sha256()
        for path in readiness.POLICY_FILES:
            digest.update(str(path).encode() + b'\0' + path.read_bytes() + b'\0')
        before = {'image': config.read_text().splitlines()[0].split('=',1)[1], 'policy': digest.hexdigest()}
        return root, config, checks, other, readiness, before

    def test_plan_predicts_only_reviewed_image_config_and_check_changes(self):
        helper = load_assets(self)
        with tempfile.TemporaryDirectory() as directory:
            root, config, checks, other, readiness, before = self.fixture(directory, helper)
            with patch.object(helper, 'CHECK_BASELINE_HASHES', {hashlib.sha256(checks.read_bytes()).hexdigest()}):
                plan = helper.plan_assets(root, readiness, before, config, checks)
            self.assertEqual(checks.read_bytes(), b'# original check\n')
            self.assertEqual(len(plan['files']), 2)
            for item in plan['files']:
                item['path'].write_bytes(item['tested'])
            self.assertEqual(other.read_bytes(), b'unchanged policy')
            digest = hashlib.sha256()
            for path in readiness.POLICY_FILES:
                digest.update(str(path).encode() + b'\0' + path.read_bytes() + b'\0')
            self.assertEqual(plan['bindings'], {'image':'localhost/new@sha256:' + 'b'*64, 'policy':digest.hexdigest()})
            self.assertEqual(checks.read_bytes(), b'# new check\n')

    def test_unknown_check_or_source_is_refused_before_changes(self):
        helper = load_assets(self)
        with tempfile.TemporaryDirectory() as directory:
            root, config, checks, other, readiness, before = self.fixture(directory, helper)
            with self.assertRaises(RuntimeError):
                helper.plan_assets(root, readiness, before, config, checks)
            with patch.object(helper, 'CHECK_BASELINE_HASHES', {hashlib.sha256(checks.read_bytes()).hexdigest()}):
                (root/'runtime/worker_entry.py').write_text('# untested\n')
                with self.assertRaises(RuntimeError):
                    helper.plan_assets(root, readiness, before, config, checks)
            self.assertEqual(checks.read_bytes(), b'# original check\n')
            self.assertEqual(config.read_text().splitlines()[0], 'IMAGE=' + before['image'])

    def test_archive_hash_is_verified_before_podman_load(self):
        helper = load_assets(self)
        with tempfile.TemporaryDirectory() as directory:
            root, config, checks, other, readiness, before = self.fixture(directory, helper)
            with patch.object(helper, 'CHECK_BASELINE_HASHES', {hashlib.sha256(checks.read_bytes()).hexdigest()}):
                plan = helper.plan_assets(root, readiness, before, config, checks)
            (root/'image.tar').write_bytes(b'changed archive')
            with patch.object(helper.subprocess, 'run') as run, self.assertRaises(RuntimeError):
                helper.prepare_images(plan)
            run.assert_not_called()

    def test_each_service_identity_must_verify_loaded_image_config_and_digest(self):
        helper = load_assets(self)
        with tempfile.TemporaryDirectory() as directory:
            root, config, checks, other, readiness, before = self.fixture(directory, helper)
            with patch.object(helper, 'CHECK_BASELINE_HASHES', {hashlib.sha256(checks.read_bytes()).hexdigest()}):
                plan = helper.plan_assets(root, readiness, before, config, checks)
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if 'load' in args:
                    self.assertEqual(kwargs['stdin'].read(), b'fixed archive')
                    return types.SimpleNamespace(stdout='loaded')
                return types.SimpleNamespace(stdout=json.dumps([{'Id':'c'*64,
                    'Digest':'sha256:'+'b'*64}]))
            with patch.object(helper.pwd, 'getpwnam', return_value=types.SimpleNamespace(pw_uid=801)), \
                 patch.object(helper.subprocess, 'run', side_effect=run):
                helper.prepare_images(plan)
            self.assertEqual([args[2] for args in calls], ['andrew-runtime','andrew-runtime','andrew-browser','andrew-browser'])
            with patch.object(helper.pwd, 'getpwnam', return_value=types.SimpleNamespace(pw_uid=801)), \
                 patch.object(helper.subprocess, 'run', return_value=types.SimpleNamespace(stdout='[{"Id":"wrong","Digest":"wrong"}]')):
                with self.assertRaises(RuntimeError):
                    helper.prepare_images(plan)
