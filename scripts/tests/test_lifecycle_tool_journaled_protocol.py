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

    def run_tool(self, name, arguments, before_resume=lambda: None, *, correction=None):
        binding, input, unused = self.prepare()
        store = self.fixture.store
        records = RecoveryRecords(store)
        records.revoke(unused['admission_id'])
        store.create_diary_entry(datetime.now(ZoneInfo('America/Toronto')).date().isoformat(), 'Synthetic diary entry.')
        input['tools'] = [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}]
        binding['input_sha'] = digest(input)
        controller = Controller(store, Mock(), 17, autonomous_enabled=True)
        self.fixture.model_gateway.recovery_tool_handler = controller.execute_tool
        def tool_response(call_id, args):
            return {'id': call_id, 'choices': [{'index': 0, 'message': {
            'role': 'assistant', 'content': None, 'tool_calls': [{'id': call_id, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(args)}}]}, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'cost': .000001}}
        responses = [tool_response('tool-call', arguments)]
        if correction is not None:
            responses.append(tool_response('correction', correction))
        self.responses[0:0] = responses
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
        self.resume_fields = fields
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
        self.assertEqual(outcome['phase'], 'terminal_committed', outcome)
        self.assertEqual(outcome['sleeping'], '0', outcome)
        self.assertEqual(outcome['admitted_tool_effects'], 0)
        self.assertEqual(outcome['provider_calls'], 2)

    def test_model_receives_rejection_and_corrects_sleep_without_duplicate_calls(self):
        outcome = self.run_tool('sleep', {'minutes': 5000}, correction={'minutes': 30})
        self.assertEqual(outcome['phase'], 'terminal_committed', outcome)
        self.assertEqual(outcome['sleeping'], '1', outcome)
        self.assertEqual(outcome['admitted_tool_effects'], 0)
        self.assertEqual(outcome['provider_calls'], 2)
        message = self.fixture.provider_calls[1]['messages'][-1]
        self.assertEqual(message['role'], 'tool')
        rejection = json.loads(json.loads(message['content']))
        self.assertEqual(rejection['status'], 'error')
        self.assertFalse(rejection['executed'])
        self.command('recovery_resume', **self.resume_fields)
        self.assertEqual(len(self.fixture.provider_calls), 2)


if __name__ == '__main__':
    unittest.main()
