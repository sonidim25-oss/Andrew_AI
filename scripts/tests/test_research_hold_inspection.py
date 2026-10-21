"""Exact pre-effect evidence only; no model/tool replay or database writes."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack, nullcontext
import subprocess

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'prototype'))
sys.path.insert(0, str(ROOT / 'scripts'))
from prototype.tests.test_recovery_dispatch import RecoveryDispatchTests
from andrewbot.recovery_records import canonical, digest
import inspect_research_hold as inspection
import apply_research_hold_reconciliation as operator
import reconcile_research_hold as repair


class ResearchHoldInspectionTests(unittest.TestCase):
    setUpFixture = RecoveryDispatchTests.setUp
    metadata = RecoveryDispatchTests.metadata

    def setUp(self):
        self.setUpFixture()
        self.home = Path(self.tmp.name) / 'home'
        self.backend = self.home / '.letta/lc-local-backend/local-recovery-v1'
        provider = self.metadata('provider', effect_id='provider-proof')
        self.gateway.complete('Bearer fixture-token', self.payload, recovery=provider)
        args = {'question': 'Synthetic question', 'findings': 'x' * 2116}
        self.tool = self.metadata('tool', effect_id='research-proof',
                                  payload={'name': 'research.save', 'arguments': args})
        with patch.object(self.controller, '_execute_tool', side_effect=ValueError('old pre-write validation')):
            with self.assertRaises(ValueError):
                self.controller.execute_tool('research.save', args, recovery=self.tool)
        self.store.transition_owner_control({
            'paused': '1', 'sleeping': '0', 'autonomy_error': 'local_recovery_interrupted',
            'active_session_id': '', 'active_cycle_id': '', 'model_turn_origin': '',
            'model_turn_progress': '', 'autonomous_owner_id': '', 'session_start_requested': '',
            'letta_agent_id': self.binding['agent_id'], 'letta_conversation_id': self.binding['conversation_id'],
        }, 'Synthetic newer pause')
        with self.store.connection:
            self.store.connection.execute('CREATE TABLE media_jobs(state TEXT,delivery TEXT)')
            self.store.connection.execute('CREATE TABLE media_avatar_proposals(state TEXT)')
        key = digest({k: self.binding[k] for k in ('agent_id', 'conversation_id')})
        directory = self.backend / 'blobs' / key
        directory.mkdir(parents=True)
        args_raw = canonical(args).encode()
        args_sha = hashlib.sha256(args_raw).hexdigest()
        (directory / args_sha).write_bytes(args_raw)
        (self.backend / 'conversations').mkdir()
        self.checkpoint_path = self.backend / 'conversations' / (key + '.json')
        self.checkpoint = {'binding': self.binding, 'phase': 'held', 'hold_reason': 'tool_uncertain',
            'held_phase': 'tool_admitted', 'revision': 10, 'claim': None, 'terminal': None,
            'steps': [{'state': 'confirmed', 'kind': 'provider', 'effect_id': 'provider-proof',
                       'admission_id': provider['admission_id'], 'request_sha': provider['effect']['request_sha'],
                       'result_ref': self.records.effect_result(provider['admission_id'])['result_sha']}],
            'tools': [{'name': 'research.save', 'state': 'admitted', 'effect_id': 'research-proof',
                       'provider_id': 'provider-proof', 'admission_id': self.tool['admission_id'],
                       'request_sha': self.tool['effect']['request_sha'], 'arguments_ref': args_sha}]}
        self.write_checkpoint()

    def write_checkpoint(self):
        self.checkpoint_path.write_text(canonical(self.checkpoint))

    def inspect(self):
        return inspection.inspect(self.path, self.home, self.tool['admission_id'])

    def test_inspection_is_read_only_and_does_not_disclose_content(self):
        before = self.store.connection.serialize()
        with patch.object(self.controller, 'execute_tool', side_effect=AssertionError('no replay')):
            proof = self.inspect()
        self.assertEqual(before, self.store.connection.serialize())
        self.assertNotIn('Synthetic question', json.dumps(proof))
        self.assertNotIn('x' * 50, json.dumps(proof))
        self.assertEqual(proof['findings_characters'], 2116)
        self.assertEqual(json.loads(proof['validation_result'])['error']['field'], 'findings')
        self.assertEqual(proof['reservations']['count'], 0)
        self.assertTrue(proof['read_only'])

    def test_owner_controls_and_restore_fences_must_match(self):
        for key, value in [('paused', '0'), ('sleeping', '1'), ('restore_recovery_required', '1'),
                           ('local_recovery_restore_required', '1'), ('owner_control_revision', '0'),
                           ('active_session_id', 'another'), ('autonomy_error', 'another')]:
            with self.subTest(key=key):
                old = self.store.get_state(key, '')
                self.store.set_state(key, value)
                try:
                    with self.assertRaises(ValueError):
                        self.inspect()
                finally:
                    self.store.set_state(key, old)

    def test_changed_checkpoint_or_request_refuses(self):
        for key, value in [('claim', {'claim_id': 'live'}), ('phase', 'tool_confirmed'),
                           ('hold_reason', 'provider_uncertain')]:
            with self.subTest(key=key):
                old = self.checkpoint[key]
                self.checkpoint[key] = value
                self.write_checkpoint()
                try:
                    with self.assertRaises(ValueError):
                        self.inspect()
                finally:
                    self.checkpoint[key] = old
                    self.write_checkpoint()
        self.checkpoint['tools'][0]['request_sha'] = 'f' * 64
        self.write_checkpoint()
        with self.assertRaises(ValueError):
            self.inspect()

    def test_existing_result_and_other_admitted_effect_are_not_ignored(self):
        original = self.tool['admission_id']
        with self.store.connection:
            row = self.store.connection.execute('SELECT * FROM recovery_effects WHERE admission_id=?', (original,)).fetchone()
            data = dict(row)
            data['admission_id'] = 'other-admission'
            data['effect_id'] = 'other-effect'
            self.store.connection.execute('INSERT INTO recovery_effects VALUES(?,?,?,?,?,?,?,?,?,?,?)', tuple(data.values()))
        with self.assertRaisesRegex(ValueError, 'Another admitted'):
            self.inspect()
        with self.store.connection:
            self.store.connection.execute("DELETE FROM recovery_effects WHERE admission_id='other-admission'")
            self.records._confirm(original, canonical('already known'), None)
        with self.assertRaises(ValueError):
            self.inspect()

    def test_operator_refuses_changed_proof_before_stopping_services(self):
        proof = self.inspect()
        proof['tool_admission_id'] = inspection.TARGET
        proof_path = Path(self.tmp.name) / 'proof.json'
        proof_path.write_text(json.dumps(proof))
        target = Path(self.tmp.name) / 'backups'
        target.mkdir()
        changed = {**proof, 'backend_revision': proof['backend_revision'] + 1}
        with patch.object(operator.os, 'geteuid', return_value=0), \
             patch.object(operator, 'verify_installed'), \
             patch.object(operator, 'BACKUPS_ROOT', target), \
             patch.object(operator, 'FENCE', target / 'fence.json'), \
             patch.object(operator, 'BOOT_MARKER', target / 'boot-marker.json'), \
             patch.object(operator, 'BOOT_GUARD', target / 'boot-guard.conf'), \
             patch.object(operator, 'inspect', return_value=changed), \
             patch.object(operator, 'load_helper'), \
             patch.object(operator, 'unit') as unit:
            with self.assertRaisesRegex(ValueError, 'Fresh inspection differs'):
                operator.apply(proof_path, digest(proof), target / 'repair')
            unit.assert_not_called()
            self.assertFalse((target / 'repair').exists())

    def test_operator_failure_is_fenced_and_exact_resume_reuses_record_phase(self):
        proof = self.inspect()
        # This coordinator test covers fail-closed service ordering. Real
        # receipt/checkpoint/projection correctness is tested against the SDK.
        proof['tool_admission_id'] = inspection.TARGET
        proof_path = Path(self.tmp.name) / 'proof.json'
        proof_path.write_text(json.dumps(proof))
        target = Path(self.tmp.name) / 'backups'
        target.mkdir()
        directory = target / 'repair'
        helper = Mock()
        helper.DEFAULT_PATHS = {}
        def snapshot(database, home, path, **kwargs):
            Path(path).mkdir()
        with ExitStack() as stack:
            stack.enter_context(patch.object(operator.os, 'geteuid', return_value=0))
            replacements = {
                'verify_installed': Mock(), 'BACKUPS_ROOT': target, 'DATABASE': self.path,
                'HOME_DIRECTORY': self.home, 'FENCE': target / 'fence.json',
                'BOOT_MARKER': target / 'boot-marker.json', 'BOOT_GUARD': target / 'boot-guard.conf',
                'REPORT': target / 'result.json', 'inspect': Mock(return_value=proof),
                'load_helper': Mock(return_value=helper), 'unit': Mock(), 'assert_stopped': Mock(),
                'create_paired_recovery_snapshot': Mock(side_effect=snapshot),
                'create_runner_recovery_snapshot': Mock(side_effect=lambda a,b,c,d,**kw: snapshot(a,b,d)),
                'execution_locks': Mock(return_value=nullcontext()), 'verify_retired': Mock(),
                'run_controller': Mock(return_value={'retired': True}),
            }
            for name, value in replacements.items():
                stack.enter_context(patch.object(operator, name, value))
            stack.enter_context(patch.object(operator.repair, 'original_checkpoint', return_value={'run_id': 'fixture'}))
            migrate = stack.enter_context(patch.object(operator.repair, 'migrate_checkpoint', side_effect=ValueError('after receipt')))
            unit, control = operator.unit, operator.run_controller
            with self.assertRaises(ValueError):
                operator.apply(proof_path, digest(proof), directory)
            self.assertTrue(operator.FENCE.exists())
            self.assertTrue(operator.BOOT_MARKER.exists())
            self.assertFalse(any(call.args[0] == 'start' for call in unit.call_args_list))
            self.assertEqual(control.call_args.args[1], 'record')
            migrate.side_effect = None
            migrate.return_value = {'result_recorded': True}
            unit.reset_mock()
            result = operator.apply(proof_path, digest(proof), directory)
            self.assertTrue(result['retired'])
            self.assertFalse(operator.FENCE.exists())
            self.assertFalse(operator.BOOT_MARKER.exists())
            self.assertFalse(operator.BOOT_GUARD.exists())
            self.assertEqual(json.loads((directory / 'private/journal.json').read_text())['phase'], 'complete')
            self.assertEqual([call.args for call in unit.call_args_list if call.args[0] == 'start'],
                [('start', 'andrew-letta.service'), ('start', 'andrew-runner.socket'),
                 ('start', 'andrew-runner.service'), ('start', 'andrew-control.service')])

    def test_timed_out_transient_gateway_is_stopped(self):
        proof_sha = 'a' * 64
        with patch.object(operator.subprocess, 'run', side_effect=[
                subprocess.TimeoutExpired('systemd-run', 120),
                subprocess.CompletedProcess(['systemctl'], 0)]) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                operator.run_controller(Path('/unused-source'), 'finalize', {}, proof_sha)
        self.assertEqual(run.call_args.args[0],
            ['systemctl', 'stop', 'andrew-research-maintenance-' + proof_sha[:12] + '.service'])

    def test_maintenance_gateway_cannot_admit_or_execute_tools_or_providers(self):
        before = self.store.connection.serialize()
        gateway = repair.evidence_gateway(self.store, 'fixture-token')
        with self.assertRaises(ValueError):
            gateway.complete('Bearer fixture-token', {})
        for path in ('/v1/recovery/authorize', '/v1/recovery/tool', '/v1/recovery/check_scope', '/v1/recovery/owner_yield'):
            self.assertEqual(gateway.recovery_request(path, {})[0], 423)
        self.assertEqual(before, self.store.connection.serialize())


del RecoveryDispatchTests
