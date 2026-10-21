"""Lifecycle tools and plain ValueError rejections inside journaled tool dispatch."""
from datetime import datetime
import json
import os
import time
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import test_local_recovery_protocol as protocol
from andrewbot.controller import Controller
from andrewbot.recovery_records import RecoveryRecords, digest


@unittest.skipUnless(os.environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'), 'requires isolated recovery release')
class LifecycleToolJournaledProtocolTests(unittest.TestCase):
    setUp = protocol.LocalRecoveryProtocolTests.setUp
    prepare = protocol.LocalRecoveryProtocolTests.prepare
    command = protocol.LocalRecoveryProtocolTests.command

    def run_tool(self, name, arguments, before_resume=lambda: None):
        binding, input, unused = self.prepare()
        store = self.fixture.store
        records = RecoveryRecords(store)
        records.revoke(unused['admission_id'])
        store.create_diary_entry(datetime.now(ZoneInfo('America/Toronto')).date().isoformat(), 'Synthetic diary entry.')
        input['tools'] = [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}]
        binding['input_sha'] = digest(input)
        controller = Controller(store, Mock(), 17, autonomous_enabled=True)
        self.fixture.model_gateway.recovery_tool_handler = controller.execute_tool
        self.responses[0:0] = [{'id': 'tool-call', 'choices': [{'index': 0, 'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'tool-call', 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}]}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'cost': .000001}}]
        scope = records.admit_scope(binding)
        admitted = self.command('recovery_admit', binding=binding, input=input, **scope)
        claim = self.command('recovery_claim', binding=binding, expected_revision=admitted['revision'], **scope)
        before_resume()
        fields = dict(binding=binding, attempt_id='lifecycle-attempt', expected_revision=claim['revision'],
                      claim_id=claim['claim_id'], fence=claim['fence'], **scope)
        self.command('recovery_resume', **fields)
        deadline = time.monotonic() + 10
        while True:
            state = self.command('recovery_status', binding=binding, attempt_id=fields['attempt_id'], **scope)
            if state.get('phase') in ('terminal_committed', 'held'):
                break
            self.assertLess(time.monotonic(), deadline, state)
            time.sleep(.03)
        return {'phase': state['phase'], 'hold_reason': state.get('hold_reason'),
                'sleeping': store.get_state('sleeping', '0'),
                'admitted_tool_effects': store.connection.execute(
                    "SELECT count(*) FROM recovery_effects WHERE kind='tool' AND state='admitted'").fetchone()[0],
                'provider_calls': len(self.fixture.provider_calls)}

    def interrupted(self):
        self.fixture.store.set_state('autonomy_error', 'local_recovery_interrupted')

    def test_control_valid_sleep_without_error_is_not_held(self):
        outcome = self.run_tool('sleep', {'minutes': 30})
        self.assertNotEqual(outcome['phase'], 'held', outcome)
        self.assertEqual(outcome['sleeping'], '1', outcome)

    def test_sleep_during_authorized_continuation_is_not_held(self):
        outcome = self.run_tool('sleep', {'minutes': 30}, self.interrupted)
        self.assertNotEqual(outcome['phase'], 'held', outcome)

    def test_continue_activity_during_authorized_continuation_is_not_held(self):
        outcome = self.run_tool('continue_activity', {}, self.interrupted)
        self.assertNotEqual(outcome['phase'], 'held', outcome)

    def test_rejected_sleep_arguments_without_any_error_state_are_not_held(self):
        outcome = self.run_tool('sleep', {'minutes': 5000})
        self.assertNotEqual(outcome['phase'], 'held', outcome)
        self.assertEqual(outcome['sleeping'], '0', outcome)


if __name__ == '__main__':
    unittest.main()
