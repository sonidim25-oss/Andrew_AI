"""Argument rejections settle exact effects; execution/authority failures do not."""
import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import Mock, patch

from prototype.tests.test_recovery_dispatch import RecoveryDispatchTests
from andrewbot.memory import MemoryService
from andrewbot.media import MediaPolicy, MediaService
from andrewbot.gmail_tools import GmailTools
from andrewbot.gmail_policy import SCOPES


class ToolRejectionRecoveryTests(unittest.TestCase):
    setUp = RecoveryDispatchTests.setUp
    metadata = RecoveryDispatchTests.metadata

    def call(self, name, arguments, effect='rejection'):
        metadata = self.metadata('tool', effect_id=effect,
                                 payload={'name': name, 'arguments': arguments})
        return metadata, self.controller.execute_tool(name, arguments, recovery=metadata)

    def test_pre_effect_rejections_are_saved_and_reused(self):
        self.controller.memory_service = MemoryService(self.store)
        self.controller.media = MediaService(self.store, Path(self.tmp.name) / 'artifacts', 17,
                                            MediaPolicy(enabled=True, spontaneous_image=True))
        self.store.create_diary_entry(datetime.now(ZoneInfo('America/Toronto')).date().isoformat(), 'Test diary.')
        cases = [
            ('sleep', {'minutes': 5000}), ('sleep', {'minutes': True}),
            ('wake_at', {'time': '25:00'}), ('sleep', {'minutes': 1, 'reason': 'x' * 501}),
            ('recent_activity', {'limit': 99}), ('check_budget', {'unknown': True}),
            ('session_search', {'query': ''}), ('session_read', {'id': -1}),
            ('research.list', {'status': 'PRIVATE'}), ('research.get', {'id': 'PRIVATE'}),
            ('research.focus', {'id': 'a' * 32}),
            ('skills.load', {'name': 'PRIVATE'}), ('media.get', {'job_id': 'PRIVATE'}),
            ('gmail.send', {}), ('memory_search', {'limit': 99}),
            ('memory_add_experience', {'content': 'ok', 'record_type': 'PRIVATE'}),
            ('memory_add_experience', {'content': 'ok', 'record_type': 'experience', 'history_ids': [999]}),
            ('memory_add_experience', {'content': 'ok', 'record_type': 'experience', 'event_ids': [[1]]}),
            ('research.delete', {'id': 'a' * 32, 'revision': 1}),
            ('image.generate', {'text': 'ok', 'request_id': 'bad id'}),
            ('image.generate', {'text': '', 'request_id': 'good-id'}),
            ('profile.propose_avatar', {'job_id': 'missing'}),
            ('continue_activity', []), ('unknown.action', {}),
        ]
        for index, (name, arguments) in enumerate(cases):
            with self.subTest(name=name, arguments=arguments):
                metadata, result = self.call(name, arguments, str(index))
                self.assertEqual(json.loads(result)['status'], 'error')
                self.assertNotIn('PRIVATE', result)
                proof = self.records.effect_result(metadata['admission_id'])
                self.assertEqual((proof['state'], proof['result']), ('confirmed', result))
                with patch.object(self.controller, '_execute_tool', side_effect=AssertionError('receipt must be reused')):
                    self.assertEqual(self.controller.execute_tool(name, arguments, recovery=metadata), result)
        self.assertEqual(self.store.get_state('sleeping', '0'), '0')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM memory_records').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM media_jobs').fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute(
            "SELECT count(*) FROM cycle_tool_attempts WHERE outcome IN ('started','uncertain')").fetchone()[0], 0)

    def test_unknown_value_error_after_commit_remains_held_and_is_not_repeated(self):
        args = {'question': 'Valid question'}
        metadata = self.metadata('tool', payload={'name': 'research.save', 'arguments': args})
        original = self.store.save_research_entry
        def write_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('PRIVATE after commit')
        with patch.object(self.store, 'save_research_entry', side_effect=write_then_fail) as save:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    self.controller.execute_tool('research.save', args, recovery=metadata)
            self.assertEqual(save.call_count, 1)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'uncertain')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0], 1)

    def test_bad_arguments_do_not_bypass_owner_or_restore_authority(self):
        for key in ('paused', 'sleeping', 'restore_recovery_required', 'owner_control_revision'):
            metadata = self.metadata('tool', effect_id=key,
                                     payload={'name': 'sleep', 'arguments': {'minutes': 5000}})
            self.store.set_state(key, '1')
            with self.assertRaises(ValueError):
                self.controller.execute_tool('sleep', {'minutes': 5000}, recovery=metadata)
            self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'uncertain')
            self.store.set_state(key, '0')

    def test_media_failure_after_queue_insert_stays_uncertain(self):
        self.controller.media = MediaService(self.store, Path(self.tmp.name) / 'artifacts', 17,
                                            MediaPolicy(enabled=True, spontaneous_image=True))
        args = {'text': 'Synthetic image', 'request_id': 'fixture-image'}
        metadata = self.metadata('tool', payload={'name': 'image.generate', 'arguments': args})
        with patch.object(self.controller.media, 'job', side_effect=ValueError('after queue insert')):
            with self.assertRaises(ValueError):
                self.controller.execute_tool('image.generate', args, recovery=metadata)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM media_jobs').fetchone()[0], 1)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'uncertain')

    def test_memory_failure_after_commit_stays_uncertain(self):
        self.controller.memory_service = MemoryService(self.store)
        args = {'content': 'Synthetic experience', 'record_type': 'experience'}
        metadata = self.metadata('tool', payload={'name': 'memory_add_experience', 'arguments': args})
        original = self.store.create_memory_record
        def write_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('after memory commit')
        with patch.object(self.store, 'create_memory_record', side_effect=write_then_fail) as write:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    self.controller.execute_tool('memory_add_experience', args, recovery=metadata)
            self.assertEqual(write.call_count, 1)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM memory_records').fetchone()[0], 1)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'uncertain')

    def test_gmail_validation_settles_but_post_send_response_failure_stays_held(self):
        self.controller.gmail_tools = GmailTools('/unused', self.store)
        api = Mock(scopes=set(SCOPES.values()))
        api.account_fingerprint.return_value = 'a' * 64
        api.request.return_value = {}  # A POST was sent; its response lacks proof.
        policy = dict(enabled=True, read=True, draft=True, send=True, delete=True)
        with patch('andrewbot.gmail_tools.Vault'), \
             patch('andrewbot.gmail_tools.GmailAPI', return_value=api), \
             patch('andrewbot.gmail_tools.load_permissions', return_value=policy):
            metadata, result = self.call('gmail.send', {'request_id': 'bad fields'})
            self.assertEqual(json.loads(result)['executed'], False)
            self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'confirmed')
            api.request.assert_not_called()
            args = dict(request_id='fixture-send', to=['fixture@example.com'], subject='Test', body='Synthetic')
            metadata = self.metadata('tool', effect_id='send', payload={'name': 'gmail.send', 'arguments': args})
            for _ in range(2):
                with self.assertRaises(ValueError):
                    self.controller.execute_tool('gmail.send', args, recovery=metadata)
            api.request.assert_called_once()
            self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'uncertain')

    def test_stale_research_delete_rejects_before_touching_children(self):
        parent = self.store.save_research_entry({'question': 'Parent'})
        child = self.store.save_research_entry({'question': 'Child', 'parent_id': parent['record_id']})
        args = {'id': parent['record_id'], 'revision': parent['revision'] + 1}
        _, result = self.call('research.delete', args)
        self.assertEqual(json.loads(result)['status'], 'error')
        after = self.store.research_entry(child['record_id'])
        self.assertEqual((after['parent_id'], after['revision']), (parent['record_id'], child['revision']))

    def test_receipt_failure_keeps_rejection_uncertain_and_does_not_repeat_callback(self):
        from andrewbot.recovery_records import RecoveryRecords
        args = {'limit': 99}
        metadata = self.metadata('tool', payload={'name': 'recent_activity', 'arguments': args})
        with patch.object(RecoveryRecords, 'confirm_tool', side_effect=OSError('receipt unavailable')):
            with self.assertRaises(OSError):
                self.controller.execute_tool('recent_activity', args, recovery=metadata)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'uncertain')
        with patch.object(self.controller, '_execute_tool', side_effect=AssertionError('must not repeat')):
            with self.assertRaises(ValueError):
                self.controller.execute_tool('recent_activity', args, recovery=metadata)


del RecoveryDispatchTests
