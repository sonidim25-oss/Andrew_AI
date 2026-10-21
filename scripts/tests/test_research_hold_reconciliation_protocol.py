"""Historical pre-write rejection: exact offline proof, no replay, paused retirement."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'prototype'))
from test_lifecycle_tool_journaled_protocol import LifecycleToolJournaledProtocolTests as Fixture
from andrewbot.appserver import AppServerClient
from andrewbot.controller import Controller
from andrewbot.memory_backup import create_paired_recovery_snapshot
from andrewbot.recovery import LocalRecoveryClient
from andrewbot.recovery_records import RecoveryRecords, canonical, digest
from andrewbot.sessions import SessionRunner
from andrewbot.gateway import GatewayThread, make_server
from andrewbot.store import Store
import inspect_research_hold as inspection
import reconcile_research_hold as repair


@unittest.skipUnless(os.environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'), 'requires isolated recovery release')
class ResearchHoldReconciliationProtocolTests(unittest.TestCase):
    setUp = Fixture.setUp
    prepare = Fixture.prepare
    command = Fixture.command
    run_tool = Fixture.run_tool

    def historical_hold(self):
        home = self.fixture.root
        self.fixture.kill()
        backend = home / '.letta/lc-local-backend'
        backend.parent.mkdir(exist_ok=True)
        if (home / 'backend').exists():
            (home / 'backend').rename(backend)
        else:
            backend.mkdir()
        self.fixture.environment['LETTA_LOCAL_BACKEND_DIR'] = str(backend)
        self.fixture.restart()
        original = Controller._execute_tool
        def old_guard(controller, name, args):
            if name == 'research.save':
                raise ValueError('Historical pre-write findings validation')
            return original(controller, name, args)
        with patch.object(Controller, '_execute_tool', old_guard):
            outcome = self.run_tool('research.save', {'question': 'Synthetic historical question', 'findings': 'x' * 2116})
        self.assertEqual(outcome['phase'], 'held')
        self.assertEqual(outcome['provider_calls'], 1)
        self.command('recovery_release', **{key: self.resume_fields[key] for key in (
            'binding', 'admission_id', 'scope_token', 'claim_id', 'fence')})
        store = self.fixture.store
        store.save_research_entry({'question': 'Existing journal A'})
        store.save_research_entry({'question': 'Existing journal B'})
        store.set_state('autonomy_error', 'local_recovery_interrupted')
        Controller(store, Mock(), 17, autonomous_enabled=True)._command('/pause')
        for key in ('active_cycle_id', 'model_turn_origin', 'model_turn_progress', 'autonomous_owner_id'):
            store.set_state(key, '')
        for key in ('agent_id', 'conversation_id'):
            store.set_state('letta_' + key, self.resume_fields['binding'][key])
        with store.connection:
            store.connection.execute('CREATE TABLE IF NOT EXISTS media_jobs(state TEXT,delivery TEXT)')
            store.connection.execute('CREATE TABLE IF NOT EXISTS media_avatar_proposals(state TEXT)')
        # Unrelated legacy effects must be retained, including held messages and
        # reservations; the reconciliation may settle only its exact tool.
        store.add_message(17, 'user', 'Unrelated held owner message', queued=3)
        with store.connection:
            store.connection.execute('INSERT INTO budget_reservations VALUES(?,?,?,?,?)',
                ('provider-' + 'f' * 64, '2026-10', '2026-10-01', .01, '2026-10-01T00:00:00+00:00'))
        effect = store.connection.execute("SELECT admission_id FROM recovery_effects WHERE state='admitted'").fetchone()[0]
        checkpoint = next(backend.glob('local-recovery-v1/conversations/*.json'))
        deadline = time.monotonic() + 5
        while json.loads(checkpoint.read_text()).get('claim'):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        proof = inspection.inspect(home / 'controller.sqlite3', home, effect)
        return home, proof

    def test_offline_retirement_is_reusable_preserves_pause_and_projects_one_error(self):
        home, proof = self.historical_hold()
        database = home / 'controller.sqlite3'
        self.fixture.kill()
        self.fixture.store.close()
        # Backup is kept outside the backend home to avoid recursively copying itself.
        snapshot = Path(self.tmp.name).parent / (Path(self.tmp.name).name + '-research-paired')
        import shutil
        self.addCleanup(lambda: shutil.rmtree(snapshot))
        create_paired_recovery_snapshot(database, home, snapshot, workers_quiescent=True)
        repair.record(database, home, proof, snapshot)
        repair.record(database, home, proof, snapshot)  # crash after receipt, before checkpoint
        before = Path(proof['checkpoint_path']).read_bytes()
        changed = json.loads(before)
        changed['revision'] += 1
        Path(proof['checkpoint_path']).write_text(canonical(changed))
        with self.assertRaises(ValueError):
            repair.migrate_checkpoint(database, home, proof, snapshot, write=True)
        self.assertEqual(Path(proof['checkpoint_path']).read_text(), canonical(changed))
        Path(proof['checkpoint_path']).write_bytes(before)
        dry = repair.migrate_checkpoint(database, home, proof, snapshot)
        self.assertEqual(before, Path(proof['checkpoint_path']).read_bytes())
        written = repair.migrate_checkpoint(database, home, proof, snapshot, write=True)
        self.assertEqual(dry['checkpoint_sha256'], written['checkpoint_sha256'])
        self.assertEqual(repair.migrate_checkpoint(database, home, proof, snapshot, write=True), written)
        self.fixture.store = Store(database)
        self.fixture.model_gateway.store = self.fixture.store
        normal_gateway = self.fixture.model_gateway
        port = self.fixture.gateway.server.server_port
        self.fixture.gateway.close()
        self.fixture.model_gateway = repair.evidence_gateway(self.fixture.store, 'fixture-token')
        self.fixture.gateway = GatewayThread(make_server(self.fixture.model_gateway, port=port))
        self.fixture.gateway.start()
        self.fixture.restart()
        client = LocalRecoveryClient(AppServerClient(self.fixture.url))
        self.addCleanup(client.close)
        with patch.object(RecoveryRecords, 'complete_scope', side_effect=ValueError('interrupted after projection')):
            with self.assertRaises(ValueError):
                repair.finalize(database, home, proof, snapshot, client=client)
        result = repair.finalize(database, home, proof, snapshot, client=client)
        self.assertTrue(result['retired'])
        repair.finalize(database, home, proof, snapshot, client=client)
        state = client.inspect(proof['binding'])
        self.assertEqual(state['phase'], 'terminal_committed')
        self.assertTrue(state['terminal']['acknowledged'])
        self.assertTrue(state['terminal']['settled'])
        self.assertEqual(len(self.fixture.provider_calls), 1)
        self.assertEqual(self.fixture.store.get_state('paused'), '1')
        self.assertEqual(self.fixture.store.get_state('owner_control_revision'), proof['guards']['owner_control_revision'])
        self.assertEqual(self.fixture.store.uncertain_message_count(17), 1)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0], 2)
        self.assertEqual(self.fixture.store.connection.execute('SELECT count(*) FROM budget_reservations').fetchone()[0], 1)
        self.assertEqual(self.fixture.store.connection.execute(
            "SELECT count(*) FROM recovery_attempts WHERE state IN ('active','held','evidence','yielding')").fetchone()[0], 0)
        history = client.client.request({'type': 'conversation_messages_list',
            'conversation_id': proof['binding']['conversation_id'], 'query': {'limit': 100, 'order': 'desc'}},
            'conversation_messages_list_response')
        self.assertEqual(json.dumps(history).count('invalid_research_arguments'), 1)
        self.fixture.gateway.close()
        self.fixture.model_gateway = normal_gateway
        self.fixture.gateway = GatewayThread(make_server(normal_gateway, port=port))
        self.fixture.gateway.start()
        # Only this explicit synthetic owner wake permits a fresh turn. It uses
        # the retired context without rerunning the historical tool/provider.
        store = self.fixture.store
        controller = Controller(store, Mock(), 17, autonomous_enabled=True)
        controller._command('/wake')
        session = SessionRunner(store, controller, Mock(), enabled=True).start('owner_wake', datetime.now(timezone.utc))
        store.begin_cycle(session, 'fresh-cycle', '2026-10-21')
        store.checkpoint_cycle_turn('fresh-cycle', 'model_started')
        runtime = {key: proof['binding'][key] for key in ('agent_id', 'conversation_id')}
        store.record_model_turn_receipt(session, 'fresh-cycle', runtime,
            {'kind': 'prepared', 'request_id': 'fresh-consumer', 'client_message_id': 'fresh-input'})
        fresh_input = {'model': 'qwen/qwen3.8-flash',
                       'messages': [{'role': 'user', 'content': 'A fresh synthetic question.'}], 'tools': []}
        fresh = {**proof['binding'], 'session_id': session, 'cycle_id': 'fresh-cycle',
                 'client_message_id': 'fresh-input', 'consumer_id': 'fresh-consumer',
                 'owner_revision': store.get_state('owner_control_revision'), 'input_sha': digest(fresh_input)}
        scope = RecoveryRecords(store).admit_scope(fresh)
        admitted = self.command('recovery_admit', binding=fresh, input=fresh_input,
            previous_binding=proof['binding'], expected_revision=client.inspect(proof['binding'])['revision'], **scope)
        claim = self.command('recovery_claim', binding=fresh, expected_revision=admitted['revision'], **scope)
        self.command('recovery_resume', binding=fresh, attempt_id='fresh-attempt',
            expected_revision=claim['revision'], claim_id=claim['claim_id'], fence=claim['fence'], **scope)
        deadline = time.monotonic() + 5
        while True:
            status = self.command('recovery_status', binding=fresh, attempt_id='fresh-attempt', **scope)
            if status['phase'] in ('terminal_committed', 'held'):
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        self.assertEqual(status['phase'], 'terminal_committed', status)
        self.assertEqual(len(self.fixture.provider_calls), 2)

    def test_changed_owner_or_checkpoint_refuses_without_mutation(self):
        home, proof = self.historical_hold()
        store = self.fixture.store
        store.set_state('owner_control_revision', str(int(proof['guards']['owner_control_revision']) + 1))
        with self.assertRaisesRegex(ValueError, 'Owner controls changed'):
            repair.protected_unchanged(store.connection, proof)
        self.assertEqual(store.connection.execute('SELECT count(*) FROM recovery_results').fetchone()[0], proof['results']['count'])


del Fixture
