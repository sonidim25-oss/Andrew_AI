"""Isolated lifecycle, storage and recovery proofs; no service credentials or sends."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backup_fixture import isolate_backup_adoption
from andrewbot.controller import Controller
from andrewbot.diary import deliver_pending_diary
from andrewbot.memory_backup import create_encrypted_snapshot, restore_snapshot
from andrewbot.store import Store
from andrewbot.tool_errors import ToolRejected


class DiaryDraftTests(unittest.TestCase):
    def setUp(self):
        isolate_backup_adoption(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'controller.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.store.set_state('active_session_id', 'first-session')
        self.controller = Controller(self.store, None, 17, autonomous_enabled=True, timezone_name='UTC')
        self.now = datetime(2026, 10, 23, 23, 50, tzinfo=timezone.utc)
        clock = self.enterContext(patch('andrewbot.controller.datetime', wraps=datetime))
        clock.now.side_effect = lambda zone: self.now.astimezone(zone)

    def tool(self, **args):
        return json.loads(self.controller.execute_tool('save_diary_entry', args))

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.controller.store = self.store

    def test_draft_revisions_remain_private_and_keep_session_and_event_provenance(self):
        first = self.store.add_activity('Observed one thing')
        second = self.store.add_activity('Observed another thing')
        draft = self.tool(content='I noticed something.', event_ids=[first])
        self.assertEqual(draft['revision'], 1)
        self.store.set_state('active_session_id', 'second-session')
        self.tool(content='I noticed more.', revision=1, event_ids=[first, second])
        self.assertEqual(self.tool(action='read', revision=1)['draft']['event_ids'], [first])
        self.assertEqual(self.tool(action='read', revision=1)['draft']['session_id'], 'first-session')
        latest = self.tool(action='read')['draft']
        self.assertEqual((latest['revision'], latest['session_id']), (2, 'second-session'))
        self.assertEqual(latest['event_ids'], [first, second])
        self.assertIsNone(self.store.diary_entry())
        self.assertEqual(self.store.pending_diary_deliveries(), [])
        self.assertIn('draft', self.tool(action='read'))

    def test_finalize_exact_revision_and_reject_further_changes_or_duplicate_finalization(self):
        event = self.store.add_activity('Observed something')
        self.tool(content='I reflected on an observation.', event_ids=[event])
        self.tool(content='I expanded the reflection.', revision=1, event_ids=[event])
        with self.assertRaises(ToolRejected):
            self.tool(action='finalize', revision=1)
        saved = self.tool(action='finalize', revision=2)
        entry = self.store.diary_entry()
        self.assertEqual(entry['entry_id'], saved['entry_id'])
        self.assertEqual(entry['content'], 'I expanded the reflection.')
        self.assertEqual(entry['event_ids'], [event])
        self.assertEqual(entry['session_id'], 'first-session')
        self.assertEqual(self.tool(action='read')['draft']['finalized_entry_id'], saved['entry_id'])
        self.assertEqual(self.store.open_diary_drafts(), [])
        for args in ({'content': 'A replacement.', 'revision': 2}, {'action': 'finalize', 'revision': 2}):
            with self.assertRaises(ToolRejected):
                self.tool(**args)
        self.assertEqual(self.store.diary_entry(), entry)
        self.assertEqual(len(self.store.pending_diary_deliveries()), 1)

    def test_unfinished_draft_crosses_midnight_and_finalizes_original_period(self):
        self.tool(content='I started reflecting before midnight.')
        self.now = datetime(2026, 10, 24, 0, 10, tzinfo=timezone.utc)
        self.reopen()
        self.assertIsNone(self.tool(action='read')['draft'])
        self.assertEqual(self.tool(action='read')['open_drafts'], [{'local_date': '2026-10-23', 'revision': 1}])
        self.tool(local_date='2026-10-23', revision=1, content='I continued reflecting after midnight.')
        self.tool(action='finalize', local_date='2026-10-23', revision=2)
        self.assertIsNotNone(self.store.diary_entry('2026-10-23'))
        self.assertIsNone(self.store.diary_entry('2026-10-24'))
        self.now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        self.assertEqual(len(self.store.diary_entries()), 1)
        self.assertEqual(self.store.open_diary_drafts(), [])

    def test_stale_edit_invalid_references_and_unsafe_text_do_not_change_draft(self):
        original = self.tool(content='An initial reflection.')
        for args in ({'content': 'Stale replacement.'}, {'content': 'Unknown event.', 'revision': 1, 'event_ids': [999]},
                     {'content': '', 'revision': 1}, {'content': 'private reasoning', 'revision': 1},
                     {'content': 'Bad refs.', 'revision': 1, 'event_ids': [[1]]},
                     {'action': 'finalize', 'revision': 0}, {'action': []}, {'action': 'finalize', 'revision': 1, 'content': 'Ignored?'},
                     {'content': 'Future.', 'local_date': '2026-10-25'}):
            with self.subTest(args=args), self.assertRaises(ToolRejected):
                self.tool(**args)
        self.assertEqual(self.tool(action='read')['draft']['content'], original['content'])
        self.assertIsNone(self.store.diary_entry())

    def test_owner_pause_at_storage_boundary_blocks_edit_and_finalization(self):
        self.tool(content='A draft before pause.')
        for action, method in [('draft', 'save_diary_draft'), ('finalize', 'finalize_diary_draft')]:
            self.store.set_state('paused', '0')
            original = getattr(self.store, method)
            peer = Store(self.path)
            try:
                def pause_then_store(*args, **kwargs):
                    peer.transition_state({'paused': '1'}, 'Owner paused fixture')
                    return original(*args, **kwargs)
                with patch.object(self.store, method, side_effect=pause_then_store):
                    with self.assertRaises(ToolRejected):
                        self.tool(action=action, revision=1, **({'content': 'Late replacement.'} if action == 'draft' else {}))
            finally:
                peer.close()
            self.assertEqual(self.store.diary_draft('2026-10-23')['revision'], 1)
            self.assertIsNone(self.store.diary_entry())

    def test_owner_revision_session_restore_and_error_gates_are_rechecked(self):
        self.tool(content='A draft.')
        for key, value in [('owner_control_revision', '1'), ('active_session_id', 'other'),
                           ('sleeping', '1'), ('restore_recovery_required', '1'), ('autonomy_error', 'failed')]:
            before = self.store.get_state(key, '')
            self.store.set_state(key, value)
            with self.assertRaises(ToolRejected):
                self.store.finalize_diary_draft('2026-10-23', 1, 'first-session', '0')
            self.store.set_state(key, before)
        self.assertIsNone(self.store.diary_entry())

    def test_finalize_failure_rolls_back_entry_and_preserves_draft(self):
        self.tool(content='A reflection to retain.')
        self.store.connection.executescript("""CREATE TRIGGER fail_diary_audit BEFORE INSERT ON activity
            WHEN NEW.event LIKE 'Diary record created%' BEGIN SELECT RAISE(ABORT,'fixture fault'); END;""")
        with self.assertRaises(sqlite3.DatabaseError):
            self.tool(action='finalize', revision=1)
        self.reopen()
        self.assertIsNone(self.store.diary_entry())
        self.assertEqual(self.store.diary_draft('2026-10-23')['revision'], 1)
        self.assertEqual(self.store.pending_diary_deliveries(), [])

    def test_encrypted_backup_restore_keeps_drafts_versions_final_entries_and_holds_delivery(self):
        event = self.store.add_activity('An objective event')
        self.tool(local_date='2026-10-22', content='A first note.', event_ids=[event])
        self.tool(local_date='2026-10-22', revision=1, content='An expanded note.', event_ids=[event])
        self.tool(content='A completed reflection.')
        final = self.tool(action='finalize', revision=1)
        snapshot = create_encrypted_snapshot(self.store, self.root / 'backups', bytes(range(32)))
        restored_path = self.root / 'restored.sqlite3'
        restore_snapshot(snapshot, restored_path, bytes(range(32)))
        restored = Store(restored_path)
        try:
            for revision in (1, 2):
                self.assertEqual(restored.diary_draft('2026-10-22', revision), self.store.diary_draft('2026-10-22', revision))
            self.assertEqual(restored.diary_entry()['entry_id'], final['entry_id'])
            self.assertEqual(restored.diary_delivery_state(final['entry_id']), 'uncertain')
            self.assertEqual(restored.pending_diary_deliveries(), [])
            self.assertEqual(restored.get_state('paused'), '1')
            self.assertEqual(restored.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            restored.close()

    def test_legacy_database_migration_preserves_finalized_entries_and_delivery_fields(self):
        entry_id = self.store.create_diary_entry('2026-10-22', 'A legacy final entry.')
        before = self.store.diary_entry('2026-10-22')
        self.store.connection.executescript('DROP TABLE diary_draft_event_refs; DROP TABLE diary_draft_revisions;')
        snapshot = create_encrypted_snapshot(self.store, self.root / 'old-backups', bytes(range(32)))
        restore_snapshot(snapshot, self.root / 'legacy-restored.sqlite3', bytes(range(32)))
        restored = Store(self.root / 'legacy-restored.sqlite3')
        try:
            self.assertEqual(restored.diary_entry('2026-10-22')['content'], before['content'])
            self.assertEqual(restored.open_diary_drafts(), [])
        finally:
            restored.close()
        self.reopen()
        self.reopen()  # additive migration is idempotent
        self.assertEqual(self.store.diary_entry('2026-10-22'), before)
        with self.assertRaises(ToolRejected):
            self.tool(local_date='2026-10-22', content='Replacement attempt.')
        self.assertEqual(self.store.diary_entry()['entry_id'], entry_id)
        self.assertEqual(self.store.open_diary_drafts(), [])

    def test_finalized_draft_uses_existing_duplicate_and_uncertain_delivery_guards(self):
        sends = []
        class Telegram:
            def send_message(inner, owner, text):
                sends.append(text)
                return {'message_id': len(sends), 'chat': {'id': owner}, 'text': text}
        self.tool(content='A final reflection.')
        self.tool(action='finalize', revision=1)
        self.assertEqual(deliver_pending_diary(Telegram(), self.store, 17), 1)
        self.reopen()
        self.assertEqual(deliver_pending_diary(Telegram(), self.store, 17), 0)
        self.assertEqual(len(sends), 1)
        self.tool(local_date='2026-10-22', content='Another reflection.')
        self.tool(action='finalize', local_date='2026-10-22', revision=1)
        class UncertainTelegram:
            def send_message(inner, owner, text):
                sends.append(text)
                raise TimeoutError()
        self.assertEqual(deliver_pending_diary(UncertainTelegram(), self.store, 17), 0)
        self.reopen()
        self.assertEqual(deliver_pending_diary(Telegram(), self.store, 17), 0)
        self.assertEqual(len(sends), 2)


from prototype.tests import test_recovery_dispatch as recovery_fixture


class DiaryRecoveryTests(unittest.TestCase):
    setUp = recovery_fixture.RecoveryDispatchTests.setUp
    metadata = recovery_fixture.RecoveryDispatchTests.metadata

    def run_diary(self, args, effect_id):
        metadata = self.metadata('tool', effect_id=effect_id,
                                 payload={'name': 'save_diary_entry', 'arguments': args})
        result = self.controller.execute_tool('save_diary_entry', args, recovery=metadata)
        return metadata, result

    def test_confirmed_draft_and_finalize_receipts_survive_restart_without_reexecution(self):
        args = {'content': 'A reflection with durable receipts.'}
        draft_meta, draft_result = self.run_diary(args, 'draft')
        draft = json.loads(draft_result)
        final_args = {'action': 'finalize', 'local_date': draft['local_date'], 'revision': 1}
        final_meta, final_result = self.run_diary(final_args, 'final')
        self.store.close()
        self.store = Store(self.path)
        self.controller.store = self.store
        for meta, arguments, expected in [(draft_meta, args, draft_result), (final_meta, final_args, final_result)]:
            with patch.object(self.controller, '_execute_tool', side_effect=AssertionError('must use receipt')):
                self.assertEqual(self.controller.execute_tool('save_diary_entry', arguments, recovery=meta), expected)
        self.assertEqual(len(self.store.diary_entries()), 1)
        self.assertEqual(len(self.store.pending_diary_deliveries()), 1)

    def test_lost_draft_or_finalize_acknowledgement_remains_uncertain_and_is_never_replayed(self):
        for action, method in [('draft', 'save_diary_draft'), ('finalize', 'finalize_diary_draft')]:
            args = {'content': 'A committed reflection.'} if action == 'draft' else {'action': 'finalize', 'revision': 1}
            metadata = self.metadata('tool', effect_id=action, payload={'name': 'save_diary_entry', 'arguments': args})
            original = getattr(self.store, method)
            def commit_then_fail(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError('fixture lost acknowledgement')
            with patch.object(self.store, method, side_effect=commit_then_fail) as mutation:
                for _ in range(2):
                    with self.assertRaises(Exception):
                        self.controller.execute_tool('save_diary_entry', args, recovery=metadata)
                self.assertEqual(mutation.call_count, 1)
            self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'uncertain')
        self.assertEqual(len(self.store.diary_entries()), 1)

    def test_validation_failure_is_saved_and_does_not_hold_a_diary_effect(self):
        self.run_diary({'content': 'A valid draft.'}, 'draft')
        metadata, result = self.run_diary({'content': 'A stale revision.'}, 'stale')
        self.assertFalse(json.loads(result)['executed'])
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'confirmed')

    def test_only_exact_interrupted_tool_scope_can_mutate_a_draft(self):
        args = {'content': 'A reflection during an admitted continuation.'}
        metadata = self.metadata('tool', effect_id='draft', payload={'name': 'save_diary_entry', 'arguments': args})
        self.store.set_state('autonomy_error', 'local_recovery_interrupted')
        with self.assertRaises(ToolRejected):
            self.controller.execute_tool('save_diary_entry', args)
        result = self.controller.execute_tool('save_diary_entry', args, recovery=metadata)
        self.assertEqual(json.loads(result)['status'], 'draft')
        final_args = {'action': 'finalize', 'revision': 1}
        with self.assertRaises(ToolRejected):
            self.controller.execute_tool('save_diary_entry', final_args)
        self.run_diary(final_args, 'final')
        self.assertEqual(self.store.get_state('autonomy_error'), 'local_recovery_interrupted')
        self.assertEqual(len(self.store.diary_entries()), 1)
