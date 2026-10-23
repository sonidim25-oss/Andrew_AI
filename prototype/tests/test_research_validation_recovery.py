"""Known pre-write research rejection is a result; unknown execution stays held."""
import json
import unittest
from unittest.mock import patch

from prototype.tests.test_recovery_dispatch import RecoveryDispatchTests


class ResearchValidationRecoveryTests(unittest.TestCase):
    setUp = RecoveryDispatchTests.setUp
    metadata = RecoveryDispatchTests.metadata

    def save(self, arguments, effect='research-save-fixture'):
        metadata = self.metadata('tool', effect_id=effect,
                                 payload={'name': 'research.save', 'arguments': arguments})
        return metadata, self.controller.execute_tool('research.save', arguments, recovery=metadata)

    def test_oversized_findings_returns_saved_error_without_research_write(self):
        arguments = {'question': 'Synthetic incident', 'findings': 'x' * 2116}
        metadata, result = self.save(arguments)
        error = json.loads(result)
        self.assertEqual(error['status'], 'error')
        self.assertEqual(error['error']['code'], 'invalid_research_arguments')
        self.assertEqual(error['error']['field'], 'findings')
        self.assertEqual(error['error']['max_characters'], 2000)
        self.assertNotIn(arguments['findings'], result)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0], 0)
        proof = self.records.effect_result(metadata['admission_id'])
        self.assertEqual(proof['state'], 'confirmed')
        self.assertEqual(proof['result'], result)
        with patch.object(self.store, 'save_research_entry', side_effect=AssertionError('must reuse receipt')):
            self.assertEqual(self.controller.execute_tool('research.save', arguments, recovery=metadata), result)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM cycle_tool_attempts').fetchone()[0], 1)

    def test_rejected_save_can_be_corrected_in_the_same_cycle(self):
        self.save({'question': 'A question', 'findings': 'x' * 2116})
        _, result = self.save({'question': 'A question', 'findings': 'x' * 2000}, effect='corrected-save')
        self.assertEqual(json.loads(result)['findings'], 'x' * 2000)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0], 1)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM recovery_effects WHERE state='admitted'").fetchone()[0], 0)

    def test_other_prewrite_validation_failures_do_not_expose_arguments(self):
        cases = [
            {'question': 'A question', 'sources': ['https://user:PRIVATE@example.org']},
            {'question': 'A question', 'parent_id': 'PRIVATE'},
            {'question': 'A question', 'status': 'PRIVATE'},
            {'question': 'A question', 'unknown': 'PRIVATE'},
            {'question': 'password: PRIVATE'},
        ]
        for index, arguments in enumerate(cases):
            with self.subTest(index=index):
                _, result = self.save(arguments, effect='invalid-save-' + str(index))
                self.assertEqual(json.loads(result)['status'], 'error')
                self.assertNotIn('PRIVATE', result)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0], 0)

    def test_blank_optional_fields_from_luna_create_an_entry(self):
        # Exact shape of the 2026-10-23 production calls: every optional field filled.
        arguments = {'id': '', 'revision': 1, 'parent_id': None, 'question': 'Can a lawful full text be found?',
                     'findings': '', 'doubts': '', 'next_step': '', 'status': 'open', 'sources': []}
        _, result = self.save(arguments)
        entry = json.loads(result)
        self.assertEqual(entry['question'], 'Can a lawful full text be found?')
        self.assertEqual((entry['revision'], entry['parent_id'], entry['sources']), (1, None, []))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0], 1)

    def test_blank_fields_in_an_edit_preserve_saved_text_and_links(self):
        _, parent = self.save({'question': 'Parent question'}, effect='parent')
        parent = json.loads(parent)
        _, child = self.save({'question': 'Child question', 'parent_id': parent['record_id'],
                              'findings': 'Saved finding', 'doubts': 'Saved doubt',
                              'sources': ['https://example.org/a']}, effect='child')
        child = json.loads(child)
        _, result = self.save({'id': child['record_id'], 'revision': 1, 'question': '', 'findings': 'New finding',
                               'doubts': '', 'next_step': '', 'parent_id': None, 'sources': [], 'status': 'open'},
                              effect='edit')
        edited = json.loads(result)
        self.assertEqual(edited['revision'], 2)
        self.assertEqual((edited['question'], edited['findings'], edited['doubts']),
                         ('Child question', 'New finding', 'Saved doubt'))
        self.assertEqual((edited['parent_id'], edited['sources']), (parent['record_id'], ['https://example.org/a']))

    def test_rejections_explain_what_to_correct_without_exposing_arguments(self):
        cases = [
            ({'id': 'octopus-arm-control-PRIVATE', 'revision': 1, 'question': 'Q?'}, 'omit id to create a new entry'),
            ({'question': '', 'findings': 'PRIVATE'}, 'a new research entry requires a question'),
            ({'question': 'Q?', 'unknown': 'PRIVATE'}, 'unsupported research fields'),
            ({'question': 'Q?', 'status': 'PRIVATE'}, 'invalid research status'),
        ]
        for index, (arguments, reason) in enumerate(cases):
            with self.subTest(index=index):
                metadata, result = self.save(arguments, effect='explained-' + str(index))
                error = json.loads(result)['error']
                self.assertEqual(error['code'], 'invalid_research_arguments')
                self.assertIn(reason, error['reason'])
                self.assertNotIn('PRIVATE', result)
                self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'confirmed')
        _, result = self.save({'question': 'Q?', 'findings': 'x' * 2001}, effect='too-long')
        error = json.loads(result)['error']
        self.assertEqual((error['field'], error['max_characters']), ('findings', 2000))
        self.assertIn('exceeds its allowed length', error['reason'])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0], 0)

    def test_stale_revision_still_reports_the_store_rejection(self):
        _, created = self.save({'question': 'Q?'}, effect='created')
        record_id = json.loads(created)['record_id']
        self.save({'id': record_id, 'revision': 1, 'findings': 'First edit'}, effect='first-edit')
        _, result = self.save({'id': record_id, 'revision': 1, 'findings': 'Stale edit'}, effect='stale-edit')
        self.assertIn('read it again before editing', json.loads(result)['error']['message'])
        self.assertEqual(self.store.research_entry(record_id)['findings'], 'First edit')

    def test_store_error_after_execution_started_remains_unknown_and_never_repeats(self):
        arguments = {'question': 'Valid question'}
        metadata = self.metadata('tool', payload={'name': 'research.save', 'arguments': arguments})
        original = self.store.save_research_entry
        def write_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('PRIVATE execution failure after commit')
        with patch.object(self.store, 'save_research_entry', side_effect=write_then_fail) as save:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    self.controller.execute_tool('research.save', arguments, recovery=metadata)
            self.assertEqual(save.call_count, 1)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'uncertain')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM research_entries').fetchone()[0], 1)

    def test_pause_blocks_before_journaling_even_a_validation_rejection(self):
        arguments = {'question': 'A question', 'findings': 'x' * 2116}
        metadata = self.metadata('tool', payload={'name': 'research.save', 'arguments': arguments})
        self.store.set_state('paused', '1')
        with self.assertRaises(ValueError):
            self.controller.execute_tool('research.save', arguments, recovery=metadata)
        self.assertEqual(self.records.effect_result(metadata['admission_id'])['state'], 'uncertain')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM cycle_tool_attempts').fetchone()[0], 0)


del RecoveryDispatchTests
