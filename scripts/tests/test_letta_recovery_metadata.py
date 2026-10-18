import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('recovery_metadata', ROOT / 'scripts/letta_recovery_metadata.py')
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)


class RecoveryMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.interrupted = self.home / '.letta/listener-state/namespace'
        self.terminal = self.home / '.letta/listener-terminal-state/namespace'
        self.interrupted.mkdir(parents=True)
        self.terminal.mkdir(parents=True)
        self.receipt = dict(cycle_id='cycle', agent_id='agent-a', conversation_id='conv-a',
                            request_id='request-a', client_message_id='input-a', run_id='run-a')
        self.main = self.interrupted / 'agent-a_conv-a.json'
        self.end = self.terminal / (base64.urlsafe_b64encode(b'["agent-a","conv-a"]').decode().rstrip('=') + '.json')
        self.record = {'agentId': 'agent-a', 'conversationId': 'conv-a', 'revision': 'revision-a',
            'toolCallIds': ['tool-a'], 'results': [{'tool_call_id': 'tool-a', 'content': 'PRIVATE tool result'}],
            'durableInputIdentities': [{'domain': 'input', 'id': 'input-a'}], 'terminalConsumerIds': ['request-a'],
            'requestOtid': 'PRIVATE request', 'workingDirectory': 'PRIVATE workspace',
            'recoveryClaimCompletion': {'state': 'running', 'lineageId': 'PRIVATE lineage'}}
        self.terminal_record = {'agentId': 'agent-a', 'conversationId': 'conv-a', 'terminals': [{
            'id': 'PRIVATE terminal ID', 'message': {'type': 'turn_finished', 'turn_id': 'PRIVATE turn',
                'stop_reason': 'end_turn', 'run_id': 'run-a', 'terminal_consumer_ids': ['request-a'],
                'error': 'PRIVATE credential'},
            'requiredConsumerIds': ['request-a'], 'acknowledgedConsumerIds': [],
            'owner': {'recoveryLineageId': 'PRIVATE authority'}}]}

    def write(self):
        self.main.write_text(json.dumps(self.record))
        self.end.write_text(json.dumps(self.terminal_record))

    def collect(self):
        return metadata.collect_recovery_metadata(self.home, [self.receipt])

    def test_matching_records_are_content_free_unverified_and_never_modified(self):
        self.write()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.main, self.end)}
        names = sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*'))
        report = self.collect()
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertFalse(report['automatic_resume'])
        self.assertFalse(report['authority_verified'])
        self.assertFalse(report['snapshot_atomic'])
        records = {item['kind']: item for item in report['records']}
        self.assertEqual(records['interrupted']['status'], 'matching_record')
        self.assertEqual(records['interrupted']['recorded_result_count'], 1)
        self.assertEqual(records['terminal']['matches'][0]['stop_reason'], 'end_turn')
        self.assertTrue(records['terminal']['matches'][0]['run_matches'])
        self.assertFalse(records['terminal']['matches'][0]['authority_verified'])
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}, before)
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*')), names)

    def test_scope_and_input_consumer_mismatches_are_not_completion_proof(self):
        for field, replacement, expected in (
                ('agentId', 'another', 'scope_mismatch'),
                ('durableInputIdentities', [{'domain': 'input', 'id': 'another'}], 'different_input'),
                ('terminalConsumerIds', ['another'], 'different_input')):
            with self.subTest(field=field):
                original = self.record[field]
                self.record[field] = replacement
                self.write()
                record = next(item for item in self.collect()['records'] if item['kind'] == 'interrupted')
                self.assertEqual(record['status'], expected)
                self.record[field] = original
        self.terminal_record['terminals'][0]['message']['terminal_consumer_ids'] = ['another']
        self.write()
        terminal = next(item for item in self.collect()['records'] if item['kind'] == 'terminal')
        self.assertEqual(terminal['status'], 'different_input')

    def test_untrusted_schema_types_and_duplicates_do_not_leak_or_crash(self):
        for value in ('{"private":"PRIVATE","private":1}', '[]', '{"bad":NaN}', '{',
                      json.dumps(self.record | {'recoveryClaimCompletion': {'state': ['PRIVATE']}}),
                      json.dumps(self.record | {'durableInputIdentities': [{'domain': ['PRIVATE'], 'id': 'x'}]})):
            with self.subTest(value=value):
                self.main.write_text(value)
                report = self.collect()
                self.assertNotIn('PRIVATE', json.dumps(report))
                self.assertIn(report['records'][0]['status'], {'invalid_record', 'invalid_json'})

    def test_symlink_file_namespace_and_home_never_follow_private_targets(self):
        outside = self.home / 'outside'
        outside.mkdir()
        secret = outside / 'secret'
        secret.write_text('PRIVATE')
        self.main.symlink_to(secret)
        self.assertEqual(self.collect()['records'][0]['status'], 'unreadable')
        self.main.unlink()
        self.interrupted.rmdir()
        self.interrupted.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.collect()['interrupted_scan'], 'some_namespaces_unavailable')
        alias = self.home / 'alias'
        alias.symlink_to(outside, target_is_directory=True)
        self.assertEqual(metadata.collect_recovery_metadata(alias, [self.receipt])['status'], 'home_unavailable')
        self.assertEqual(secret.read_text(), 'PRIVATE')

    def test_fifo_and_oversized_file_are_bounded_without_blocking(self):
        os.mkfifo(self.main)
        self.assertEqual(self.collect()['records'][0]['status'], 'unsafe_file')
        self.main.unlink()
        with self.main.open('wb') as stream:
            stream.truncate(metadata.MAX_BYTES + 1)
        self.assertEqual(self.collect()['records'][0]['status'], 'too_large')

    def test_missing_journal_is_not_a_successful_turn(self):
        self.assertEqual(self.collect()['records'], [])
        self.assertFalse(self.collect()['automatic_resume'])
        self.assertEqual(metadata.collect_recovery_metadata(self.home / 'missing', [self.receipt])['status'], 'home_unavailable')

    def test_legacy_session_without_receipts_does_not_claim_journals_were_inspected(self):
        with patch.object(metadata, '_open_directory', side_effect=AssertionError('no bound inputs to inspect')):
            report = metadata.collect_recovery_metadata(self.home, [])
        self.assertEqual(report['status'], 'no_controller_receipts')
        self.assertEqual(report['controller_receipt_count'], 0)
        self.assertEqual(report['records'], [])
        self.assertFalse(report['automatic_resume'])
        for kind in ('interrupted', 'terminal', 'input'):
            self.assertEqual(report[kind + '_scan'], 'not_attempted')

    def test_input_disposition_binds_client_identity_and_never_certifies_replay_markers(self):
        folder = self.home / '.letta/state/namespace'
        folder.mkdir(parents=True)
        path = folder / 'input-dispositions-v2.json'
        runtime_key = 'agent:agent-a::conversation:conv-a'
        key = json.dumps([runtime_key, 'input', 'input-a'], separators=(',', ':'))
        entry = {'runtimeKey': runtime_key, 'disposition': 'started', 'replayCompleted': True,
                 'completedTerminalAuthority': {'private': 'PRIVATE authority'},
                 'preparedTerminal': {'scope': {'agentId': 'agent-a', 'conversationId': 'conv-a'},
                    'message': self.terminal_record['terminals'][0]['message']}}
        record = {'version': 4, 'entries': {key: entry}}
        path.write_text(json.dumps(record))
        result = self.collect()
        item = result['records'][0]
        self.assertEqual(item['status'], 'matching_record')
        self.assertTrue(item['terminal_consumer_matches'])
        self.assertTrue(item['replay_completed_marker'])
        self.assertFalse(item['authority_verified'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        entry['preparedTerminal']['scope']['agentId'] = 'other'
        path.write_text(json.dumps(record))
        self.assertEqual(self.collect()['records'][0]['status'], 'invalid_record')
        path.write_text(json.dumps({'version': 4, 'entries': {key + 'other': entry}}))
        self.assertEqual(self.collect()['records'][0]['status'], 'different_input')
        path.write_text(json.dumps({'version': 5, 'entries': {key: entry}}))
        self.assertEqual(self.collect()['records'][0]['status'], 'unsupported_record')

    def test_receipt_and_namespace_limits_are_explicit(self):
        self.write()
        receipts = [self.receipt] * (metadata.MAX_RECEIPTS + 1)
        self.assertTrue(metadata.collect_recovery_metadata(self.home, receipts)['receipt_limit'])
        with patch.object(metadata, 'MAX_NAMESPACES', 0):
            report = self.collect()
        self.assertEqual(report['interrupted_scan'], 'namespace_limit')
        self.assertEqual(report['records'], [])

    def test_diagnostic_cli_reads_sqlite_and_journals_without_mutating_state(self):
        sys.path.insert(0, str(ROOT / 'prototype'))
        from andrewbot.store import Store
        db_path = self.home / 'controller.db'
        store = Store(db_path)
        with store.connection:
            store.connection.execute("INSERT INTO cycles(cycle_id,session_id,local_day,status,started_at) VALUES('cycle','0123456789abcdef','2026-10-18','failed','2026-10-18T00:00:00+00:00')")
            store.connection.execute("INSERT INTO model_turn_receipts(cycle_id,session_id,owner_revision,agent_id,conversation_id,request_id,client_message_id,state,created_at,updated_at) VALUES('cycle','0123456789abcdef','1','agent-a','conv-a','request-a','input-a','accepted','2026-10-18T00:00:00+00:00','2026-10-18T00:00:00+00:00')")
            store.connection.execute("INSERT INTO budget_reservations VALUES('old-reservation','2026-09','2026-09-01',0.01,'2026-09-01T00:00:00+00:00')")
        store.set_state('paused', '1')
        store.close()
        self.write()
        before = db_path.read_bytes()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/diagnose-session.py'), '--database', str(db_path),
            '--letta-recovery-metadata', '--letta-home', str(self.home), '--brief'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['letta_recovery_metadata']['status'], 'metadata_only')
        self.assertEqual(report['unsettled_reservations'],1)
        self.assertEqual(report['global_reservation_count'],1)
        self.assertEqual(report['global_reservations'][0]['reservation_id'],'old-reservation')
        self.assertNotIn('PRIVATE', result.stdout + result.stderr)
        self.assertEqual(db_path.read_bytes(), before)
