"""Known research rejection continues through the actual isolated backend."""
import json
import os
import time
import unittest
from unittest.mock import Mock

import test_local_recovery_protocol as protocol
from andrewbot.controller import Controller
from andrewbot.recovery_records import RecoveryRecords, digest


@unittest.skipUnless(os.environ.get('ANDREW_LOCAL_RECOVERY_TEST_RELEASE'), 'requires isolated recovery release')
class ResearchValidationProtocolTests(unittest.TestCase):
    setUp = protocol.LocalRecoveryProtocolTests.setUp
    prepare = protocol.LocalRecoveryProtocolTests.prepare
    command = protocol.LocalRecoveryProtocolTests.command

    def test_rejected_save_is_confirmed_then_corrected_without_holding_the_turn(self):
        binding, input, unused = self.prepare()
        records = RecoveryRecords(self.fixture.store)
        records.revoke(unused['admission_id'])
        input['tools'] = [{'type': 'function', 'function': {
            'name': 'research.save', 'parameters': {'type': 'object'}}}]
        binding['input_sha'] = digest(input)
        controller = Controller(self.fixture.store, Mock(), 17, autonomous_enabled=True)
        self.fixture.model_gateway.recovery_tool_handler = controller.execute_tool
        def response(call_id, findings):
            return {'id': call_id, 'choices': [{'index': 0, 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': call_id, 'type': 'function', 'function': {
                        'name': 'research.save', 'arguments': json.dumps({
                            'question': 'Synthetic incident reproduction', 'findings': findings})}}]},
                'finish_reason': 'tool_calls'}],
                'usage': {'prompt_tokens': 20, 'completion_tokens': 5, 'cost': .000001}}
        self.responses[0:0] = [response('oversized-findings', 'x' * 2116),
                               response('corrected-findings', 'Short valid findings.')]
        scope = records.admit_scope(binding)
        admitted = self.command('recovery_admit', binding=binding, input=input, **scope)
        claim = self.command('recovery_claim', binding=binding,
                             expected_revision=admitted['revision'], **scope)
        fields = dict(binding=binding, attempt_id='research-validation-attempt',
                      expected_revision=claim['revision'], claim_id=claim['claim_id'], fence=claim['fence'], **scope)
        self.command('recovery_resume', **fields)
        deadline = time.monotonic() + 10
        while True:
            state = self.command('recovery_status', binding=binding,
                                 attempt_id=fields['attempt_id'], **scope)
            if state.get('phase') in ('terminal_committed', 'held'):
                break
            self.assertLess(time.monotonic(), deadline, state)
            time.sleep(.03)
        self.assertEqual(state['phase'], 'terminal_committed', state)
        self.assertEqual(len(self.fixture.provider_calls), 3)
        error_message = self.fixture.provider_calls[1]['messages'][-1]
        self.assertEqual(error_message['role'], 'tool')
        rejection = json.loads(json.loads(error_message['content']))
        self.assertEqual(rejection['error']['field'], 'findings')
        self.assertEqual(rejection['error']['max_characters'], 2000)
        rows = self.fixture.store.connection.execute('SELECT findings FROM research_entries').fetchall()
        self.assertEqual([row[0] for row in rows], ['Short valid findings.'])
        self.assertEqual(self.fixture.store.connection.execute(
            "SELECT count(*) FROM recovery_effects WHERE state='admitted'").fetchone()[0], 0)
        self.command('recovery_resume', **fields)
        self.assertEqual(len(self.fixture.provider_calls), 3)
