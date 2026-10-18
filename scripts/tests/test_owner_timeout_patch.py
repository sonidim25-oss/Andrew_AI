import ast
import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('owner_timeout_patch', ROOT / 'scripts/fix-owner-turn-timeout.py')
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


class OwnerTimeoutPatchTests(unittest.TestCase):
    def source(self, timeout):
        source = (ROOT / 'prototype/andrewbot/appserver.py').read_text()
        tree = ast.parse(source)
        reply = next(node for cls in tree.body if isinstance(cls, ast.ClassDef)
                     and cls.name == 'LettaAgent' for node in cls.body
                     if isinstance(node, ast.FunctionDef) and node.name == 'reply')
        defaults = dict(zip([arg.arg for arg in reply.args.args][-len(reply.args.defaults):],
                            reply.args.defaults))
        default = defaults['timeout']
        lines = source.splitlines(keepends=True)
        line = lines[default.lineno - 1].encode()
        lines[default.lineno - 1] = (line[:default.col_offset] + str(timeout).encode()
                                    + line[default.end_col_offset:]).decode()
        return ''.join(lines).encode()

    def test_changes_only_owner_default_for_deployed_timeout_values(self):
        for timeout in (120, 600):
            with self.subTest(timeout=timeout):
                original = self.source(timeout)
                patched, previous = patcher.patch_source(original)
                self.assertEqual(previous, timeout)
                self.assertEqual(patched, original.replace(
                    f'def reply(self, messages, timeout={timeout}'.encode(),
                    b'def reply(self, messages, timeout=None'))
                self.assertIn(b'def send_user_turn(self, runtime, text, tool_handler=None, timeout=120):', patched)
                ast.parse(patched)

    def test_refuses_missing_unlimited_socket_support(self):
        source = self.source(120).replace(b'self.socket.settimeout(None)', b'self.socket.settimeout(120)')
        with self.assertRaisesRegex(ValueError, 'Downstream'):
            patcher.patch_source(source)

    def test_refuses_to_repatch_or_guess_at_a_changed_signature(self):
        with self.assertRaisesRegex(ValueError, 'Expected a positive'):
            patcher.patch_source(self.source('None'))

    def exercise_install(self, fail_acceptance=False, fail_start=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'appserver.py'
            original = self.source(120)
            target.write_bytes(original)
            proof_path = root / 'runtime-proof.json'
            proof_path.write_bytes(b'{"schema": 1}')
            backup = root / 'backup'
            backup.mkdir()
            helper = Mock()
            readiness = helper.load_readiness.return_value
            readiness.PROOF = proof_path
            readiness.protected_read.return_value = proof_path.read_bytes()
            readiness.bindings.return_value = {'code': 'original', 'policy': 'unchanged'}
            if fail_acceptance:
                helper.acceptance.side_effect = RuntimeError('acceptance fixture failure')
            startup = Mock(side_effect=[RuntimeError('startup fixture failure'), None] if fail_start else None)
            events = Mock()
            events.attach_mock(helper, 'helper')
            events.attach_mock(startup, 'startup')
            with patch.object(patcher, 'TARGET', target), \
                 patch.object(patcher, 'EXPECTED', hashlib.sha256(original).hexdigest()), \
                 patch.object(patcher.os, 'geteuid', return_value=0), \
                 patch.object(patcher, 'require_halted'), \
                 patch.object(patcher, 'deployment_helpers', return_value=helper), \
                 patch.object(patcher, 'acceptance_snapshot', return_value=root), \
                 patch.object(patcher.tempfile, 'mkdtemp', return_value=str(backup)), \
                 patch.object(patcher, 'write_module', side_effect=lambda data, _: target.write_bytes(data)), \
                 patch.object(patcher, 'command') as command, \
                 patch.object(patcher, 'start_services', startup), \
                 patch('sys.argv', ['fix-owner-turn-timeout.py', '--apply']), patch('builtins.print'):
                if fail_acceptance or fail_start:
                    with self.assertRaises(RuntimeError):
                        patcher.main()
                    self.assertEqual(target.read_bytes(), original)
                else:
                    patcher.main()
                    self.assertEqual(target.read_bytes(), patcher.patch_source(original)[0])
                if fail_acceptance:
                    command.assert_not_called()
                    startup.assert_not_called()
                    helper.renew_proof.assert_not_called()
                else:
                    names = [call[0] for call in events.mock_calls]
                    self.assertLess(names.index('helper.acceptance'), names.index('helper.renew_proof'))
                    self.assertLess(names.index('helper.renew_proof'), names.index('startup'))
                    helper.check_idle.assert_called()
                    if fail_start:
                        helper.replace.assert_called_once_with(proof_path, b'{"schema": 1}',
                                                              proof_path.stat().st_gid,
                                                              mode=proof_path.stat().st_mode & 0o777)
                        readiness.validate_runtime_proof.assert_called_once()
                        self.assertEqual(startup.call_count, 2)

    def test_acceptance_and_proof_renewal_precede_controller_restart(self):
        self.exercise_install()

    def test_failed_acceptance_changes_no_services_or_code(self):
        self.exercise_install(fail_acceptance=True)

    def test_failed_startup_restores_code_and_proof_before_restarting(self):
        self.exercise_install(fail_start=True)

    def test_acceptance_snapshot_uses_deployed_modules_with_only_proposed_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'installed/prototype/andrewbot'
            package.mkdir(parents=True)
            target = package / 'appserver.py'
            target.write_bytes(b'old client')
            (package / 'controller.py').write_bytes(b'# deployed controller\n')
            skills = root / 'installed/skills'
            skills.mkdir()
            (skills / 'example.md').write_text('Installed guidance')
            backup = root / 'backup'
            backup.mkdir()
            with patch.object(patcher, 'TARGET', target):
                snapshot = patcher.acceptance_snapshot(backup, b'# proposed client\n')
            self.assertEqual((snapshot / 'prototype/andrewbot/appserver.py').read_bytes(), b'# proposed client\n')
            self.assertEqual((snapshot / 'prototype/andrewbot/controller.py').read_bytes(), b'# deployed controller\n')
            self.assertEqual((snapshot / 'skills/example.md').read_text(), 'Installed guidance')
            self.assertTrue((snapshot / 'scripts/tests/test_timeout_recovery_protocol.py').is_file())

if __name__ == '__main__':
    unittest.main()
