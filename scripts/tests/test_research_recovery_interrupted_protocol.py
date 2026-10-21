"""research.save during an authorized journaled continuation after local_recovery_interrupted."""
import json
import os
import time
import unittest
from unittest.mock import Mock

import test_local_recovery_protocol as protocol
from andrewbot.appserver import LettaCommandError
from andrewbot.controller import Controller
from andrewbot.recovery_records import RecoveryRecords, digest


@unittest.skipUnless(os.environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'), 'requires isolated recovery release')
class ResearchRecoveryInterruptedProtocolTests(unittest.TestCase):
    setUp = protocol.LocalRecoveryProtocolTests.setUp
    prepare = protocol.LocalRecoveryProtocolTests.prepare
    command = protocol.LocalRecoveryProtocolTests.command

    def run_save(self, before_resume):
        binding, input, unused = self.prepare()
        records = RecoveryRecords(self.fixture.store)
        records.revoke(unused['admission_id'])
        input['tools'] = [{'type': 'function', 'function': {'name': 'research.save', 'parameters': {'type': 'object'}}}]
        binding['input_sha'] = digest(input)
        controller = Controller(self.fixture.store, Mock(), 17, autonomous_enabled=True)
        self.fixture.model_gateway.recovery_tool_handler = controller.execute_tool
        self.responses[0:0] = [{'id': 'save-call', 'choices': [{'index': 0, 'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'save-call', 'type': 'function', 'function': {
                    'name': 'research.save', 'arguments': json.dumps({
                        'question': 'Synthetic continuation question', 'findings': 'Valid short findings.'})}}]},
            'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'cost': .000001}}]
        scope = records.admit_scope(binding)
        admitted = self.command('recovery_admit', binding=binding, input=input, **scope)
        claim = self.command('recovery_claim', binding=binding, expected_revision=admitted['revision'], **scope)
        before_resume()
        fields = dict(binding=binding, attempt_id='interrupted-continuation', expected_revision=claim['revision'],
                      claim_id=claim['claim_id'], fence=claim['fence'], **scope)
        try:
            self.command('recovery_resume', **fields)
        except LettaCommandError as error:
            return {'resume_refused': error.args[0] if error.args else str(error), 'entries': [
                row[0] for row in self.fixture.store.connection.execute('SELECT findings FROM research_entries')],
                'provider_calls': len(self.fixture.provider_calls)}
        deadline = time.monotonic() + 10
        while True:
            state = self.command('recovery_status', binding=binding, attempt_id=fields['attempt_id'], **scope)
            if state.get('phase') in ('terminal_committed', 'held'):
                break
            self.assertLess(time.monotonic(), deadline, state)
            time.sleep(.03)
        store = self.fixture.store
        return {'phase': state['phase'], 'hold_reason': state.get('hold_reason'),
                'entries': [row[0] for row in store.connection.execute('SELECT findings FROM research_entries')],
                'admitted_tool_effects': store.connection.execute(
                    "SELECT count(*) FROM recovery_effects WHERE kind='tool' AND state='admitted'").fetchone()[0],
                'provider_calls': len(self.fixture.provider_calls),
                'autonomy_error': store.get_state('autonomy_error', '')}

    def test_control_without_interruption_saves_and_completes(self):
        outcome = self.run_save(lambda: None)
        self.assertEqual(outcome['phase'], 'terminal_committed', outcome)
        self.assertEqual(outcome['entries'], ['Valid short findings.'])

    def test_authorized_continuation_after_interruption_saves_and_completes(self):
        outcome = self.run_save(lambda: self.fixture.store.set_state('autonomy_error', 'local_recovery_interrupted'))
        self.assertEqual(outcome['phase'], 'terminal_committed', outcome)
        self.assertEqual(outcome['entries'], ['Valid short findings.'], outcome)
        self.assertEqual(outcome['admitted_tool_effects'], 0, outcome)

    def test_owner_pause_still_blocks_the_write(self):
        def pause():
            self.fixture.store.set_state('autonomy_error', 'local_recovery_interrupted')
            self.fixture.store.set_state('paused', '1')
        outcome = self.run_save(pause)
        self.assertIn('owner_blocked', outcome.get('resume_refused', ''), outcome)
        self.assertEqual((outcome['entries'], outcome['provider_calls']), ([], 0), outcome)

    def test_other_session_errors_still_block_the_write(self):
        outcome = self.run_save(lambda: self.fixture.store.set_state('autonomy_error', 'provider_attempt_uncertain'))
        self.assertIn('owner_blocked', outcome.get('resume_refused', ''), outcome)
        self.assertEqual((outcome['entries'], outcome['provider_calls']), ([], 0), outcome)


if __name__ == '__main__':
    unittest.main()
