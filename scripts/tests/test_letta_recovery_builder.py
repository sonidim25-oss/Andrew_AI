import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class RecoveryBuilderTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('recovery_builder', ROOT / 'scripts/build-letta-recovery.py')
        self.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.builder)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / 'base'
        self.base.mkdir()
        patches = json.loads((ROOT / 'letta_recovery/patches.json').read_text())
        self.source = ('#!/usr/bin/env node\n' + '\n'.join(patch['anchor'] for patch in patches) + '\n').encode()
        (self.base / 'letta.js').write_bytes(self.source)
        (self.base / 'package.json').write_text(json.dumps({'name':'@letta-ai/letta-code','version':'0.34.13','type':'module'}))
        (self.base / 'LICENSE').write_text('Fixture license')
        (self.base / 'npm-shrinkwrap.json').write_text('{"lockfileVersion":3}')
        (self.base / 'node_modules').mkdir()
        self.pin = patch.object(self.builder, 'BASE_SHA', hashlib.sha256(self.source).hexdigest())
        self.pin.start(); self.addCleanup(self.pin.stop)

    def test_build_refuses_modified_or_ambiguous_base(self):
        (self.base / 'letta.js').write_bytes(self.source + b'changed')
        with self.assertRaisesRegex(ValueError, 'base hash'):
            self.builder.build_release(self.base, self.root / 'release')
        doubled = self.source + self.source
        (self.base / 'letta.js').write_bytes(doubled)
        with patch.object(self.builder, 'BASE_SHA', hashlib.sha256(doubled).hexdigest()):
            with self.assertRaisesRegex(ValueError, 'anchor'):
                self.builder.build_release(self.base, self.root / 'ambiguous')

    def test_build_is_repeatable_and_leaves_npm_package_unchanged(self):
        before = {p.relative_to(self.base).as_posix():p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        first = self.builder.build_release(self.base, self.root / 'first')
        second = self.builder.build_release(self.base, self.root / 'second')
        self.assertEqual(first, second)
        self.assertEqual(first['contract'], 'local_durable_recovery_v1')
        self.assertEqual(first['base_sha256'], hashlib.sha256(self.source).hexdigest())
        self.assertNotEqual(first['files']['letta.js'], first['base_sha256'])
        after = {p.relative_to(self.base).as_posix():p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual((self.root / 'first/LICENSE').read_text(), 'Fixture license')

    def test_external_dependency_symlink_is_rejected(self):
        (self.base / 'node_modules/escape').symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.builder.build_release(self.base, self.root / 'release')


class RecoveryArtifactStartupTests(unittest.TestCase):
    @unittest.skipUnless(__import__('os').environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'), 'requires isolated recovery release')
    def test_inactive_release_starts_without_recovery_or_effects(self):
        import os
        spec = importlib.util.spec_from_file_location('recovery_protocol_fixture', ROOT / 'letta_recovery/tests/protocol_fixture.py')
        fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture_module)
        with tempfile.TemporaryDirectory() as directory:
            fixture = fixture_module.launch_release(os.environ['ANDREW_LOCAL_RECOVERY_TEST_RELEASE'], directory)
            try:
                reply = fixture.client.request({'type':'app_server_info'}, 'app_server_info_response')
                self.assertNotIn('local_durable_recovery_v1', json.dumps(reply))
                self.assertEqual(fixture.provider_calls, [])
                self.assertFalse((Path(directory) / 'local-recovery-v1').exists())
            finally:
                fixture.stop()
