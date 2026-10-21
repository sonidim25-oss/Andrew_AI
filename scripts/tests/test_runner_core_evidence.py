"""A passing core result can be reused only for identical validated inputs."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class CoreEvidenceTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('core_evidence_installer', ROOT/'scripts/install-runner-recovery.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'source'
        for part in ('prototype/andrewbot', 'prototype/tests', 'skills', 'letta_recovery', 'runtime'):
            directory = self.source/part
            directory.mkdir(parents=True)
            (directory/'fixture.txt').write_text('reviewed input')
        self.log = self.root/'core.txt'
        self.log.write_text('Ran 709 tests in 107.619s\n\nOK (skipped=2)\n')
        self.proof = self.root/'core-evidence.json'

    def write_proof(self):
        self.proof.write_text(json.dumps({
            'contract': 'runner_core_acceptance_evidence_v1',
            'inputs': self.module.core_acceptance_inputs(self.source),
            'python_version': sys.version,
            'python_sha256': self.module.sha(sys.executable),
            'log_sha256': self.module.sha(self.log),
            'command': ['python3', '-m', 'unittest', 'discover', '-s', 'prototype/tests', '-q'],
            'tested_commit': '534d6f40118502711aa8784ad4f7b78ea5a4246d',
        }))

    def test_identical_inputs_reuse_exact_passing_log(self):
        self.write_proof()
        raw, proof = self.module.reuse_core_acceptance(self.source, self.proof)
        self.assertEqual(raw, self.log.read_bytes())
        self.assertEqual(proof['inputs'], self.module.core_acceptance_inputs(self.source))

    def test_changed_code_tests_skills_or_backend_refuse_reuse(self):
        for part in ('prototype/andrewbot', 'prototype/tests', 'skills', 'letta_recovery', 'runtime'):
            with self.subTest(part=part):
                self.write_proof()
                path = self.source/part/'fixture.txt'
                path.write_text('changed after verification')
                with self.assertRaisesRegex(ValueError, 'inputs'):
                    self.module.reuse_core_acceptance(self.source, self.proof)
                path.write_text('reviewed input')

    def test_changed_log_failed_log_and_wrong_python_refuse_reuse(self):
        self.write_proof()
        self.log.write_text('FAILED (failures=1)\n')
        with self.assertRaises(ValueError):
            self.module.reuse_core_acceptance(self.source, self.proof)
        self.write_proof()
        with self.assertRaises(ValueError):
            self.module.reuse_core_acceptance(self.source, self.proof)
        self.log.write_text('Ran 709 tests in 107.619s\n\nOK (skipped=2)\n')
        self.write_proof()
        proof = json.loads(self.proof.read_text());proof['python_version'] = 'different interpreter'
        self.proof.write_text(json.dumps(proof))
        with self.assertRaises(ValueError):
            self.module.reuse_core_acceptance(self.source, self.proof)

    def test_reuse_skips_only_core_and_keeps_other_acceptance_jobs(self):
        self.write_proof()
        directory = self.root/'candidate';directory.mkdir()
        candidate = {'directory': str(directory), 'source': str(self.source),
                     'backend_release': str(self.root/'release')}
        (directory/'candidate.json').write_text(json.dumps(candidate))
        pristine = self.root/'pristine';pristine.write_text('fixture')
        expected = json.loads((ROOT/'letta_recovery/base.json').read_text())['sha256']
        real_sha = self.module.sha
        def sha(path):
            return expected if Path(path) == pristine else real_sha(path)
        with patch.object(self.module, 'verify_candidate'), \
             patch.object(self.module, 'sha', side_effect=sha), \
             patch.object(self.module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as run:
            record = self.module.run_acceptance(candidate, pristine, core_evidence=self.proof)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 4)
        self.assertFalse(any('prototype/tests' in command for command in commands))
        self.assertEqual(set(record['checks']), {'node', 'core', 'scripts', 'pristine_protocol', 'service_imports'})
        self.assertEqual((directory/'core-acceptance.txt').read_bytes(), self.log.read_bytes())
        self.assertIn('core', record['reused_checks'])
        self.assertIn('core-reused-evidence.json', record['logs'])
