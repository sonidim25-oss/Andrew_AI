"""Known pre-write research rejection is a result; unknown execution stays held."""
import json
import unittest
from unittest.mock import patch

from test_recovery_dispatch import RecoveryDispatchTests


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
