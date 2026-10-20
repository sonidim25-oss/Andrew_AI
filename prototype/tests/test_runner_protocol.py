import asyncio
import copy
import io
import json
import struct
import unittest
from dataclasses import FrozenInstanceError

from andrewbot.recovery_identity import canonical, digest
from andrewbot.runner_protocol import (
    RunnerProtocolError, decode_frame, encode_frame, read_frame, read_frame_async,
    validate_evidence, validate_grant, validate_identity,
)


def identity_document():
    binding = dict(store_id='backend-store', agent_id='agent', conversation_id='conversation',
                   session_id='session', cycle_id='cycle', client_message_id='input',
                   consumer_id='consumer', owner_revision='36', input_sha='a' * 64,
                   config_sha='b' * 64)
    return dict(contract='runner_result_recovery_v1', version=1,
                controller_store_id='01234567-1234-4321-8123-123456789abc',
                runner_store_id='11234567-1234-4321-8123-123456789abc',
                binding=binding, binding_sha=digest(binding), admission_id='c' * 64,
                effect_id='t-' + 'd' * 64, request_sha='e' * 64, operation_id='f' * 64,
                legacy_attempt_id='attempt', tool_name='python.run', worker_request_sha='1' * 64,
                formatter='runner_text_v1', research_context=dict(
                    session_id='session', cycle_id='cycle', research_id='', research_revision=0))


class RunnerProtocolTests(unittest.TestCase):
    def test_canonical_hashes_unchanged(self):
        from andrewbot import recovery_records
        fixtures = [
            ({'z': [None, True, False, 1, -0.0, 1e-7, 1e20, 1e21], 'é': '𝄞', '😀': {'a': 'ñ'}},
             '4bd1e57829f4e62343f488f13137f81bd8d40cb168c837ac913788b2f0fcbf60'),
            ({'nested': {'β': 2, 'a': [1.5, 1e-6, -12.25]}, 'unicode': '雪'},
             '73bcef6b2a0973287ead0eecb52703143d8fc5c2d8dc0eec37cfd62c77d556fe'),
        ]
        for value, expected in fixtures:
            self.assertEqual(digest(value), expected)
            self.assertEqual(recovery_records.digest(value), expected)
            self.assertEqual(recovery_records.canonical(value), canonical(value))

    def test_identity_is_deeply_immutable_and_copies_input(self):
        document = identity_document()
        identity = validate_identity(document)
        document['binding']['agent_id'] = 'changed'
        self.assertEqual(identity.binding['agent_id'], 'agent')
        with self.assertRaises(TypeError):
            identity.binding['agent_id'] = 'changed'
        with self.assertRaises(TypeError):
            identity.research_context['research_revision'] = 7
        with self.assertRaises(FrozenInstanceError):
            identity.operation_id = 'a' * 64
        self.assertEqual(validate_identity(identity.to_dict()), identity)

    def test_unknown_fields_and_invalid_identity_rejected(self):
        for changes in [dict(extra='PRIVATE'), dict(version=True), dict(operation_id='X' * 64),
                        dict(controller_store_id='arbitrary'), dict(binding_sha='a' * 64),
                        dict(tool_name='browser.open'), dict(formatter='new'),
                        dict(research_context={'session_id': 'other'})]:
            with self.subTest(changes=changes), self.assertRaises(RunnerProtocolError) as error:
                validate_identity({**identity_document(), **changes})
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_grant_is_strict_and_bound_to_operation(self):
        value = dict(operation_sha='a' * 64, challenge='b' * 64, scope_id='scope', fence=1,
                     boot_id='01234567-1234-4321-8123-123456789abc', expires_monotonic_ms=1250.5)
        self.assertEqual(validate_grant(value).to_dict(), value)
        for changed in [dict(fence=True), dict(fence=0), dict(expires_monotonic_ms=float('nan')),
                        dict(extra='PRIVATE'), dict(challenge='bad')]:
            with self.assertRaises(RunnerProtocolError):
                validate_grant({**value, **changed})

    def test_state_dependent_evidence_and_exact_identity(self):
        identity = validate_identity(identity_document())
        base = dict(contract=identity.contract, version=1, identity=identity.to_dict(),
                    identity_sha=digest(identity.to_dict()), state='missing',
                    response=None, response_sha=None, cleanup_verified=False)
        for state in ('missing', 'prepared', 'admitted', 'uncertain'):
            self.assertEqual(validate_evidence({**base, 'state': state}, identity).state, state)
        response = dict(status='ok', stdout='42', stderr='', artifacts=[], safe_error=None, visit=None)
        saved = {**base, 'state': 'result', 'response': response, 'response_sha': digest(response),
                 'cleanup_verified': True}
        receipt = validate_evidence(saved, identity)
        self.assertTrue(receipt.importable)
        saved['response']['stdout'] = 'changed'
        self.assertEqual(receipt.to_dict()['response']['stdout'], '42')
        for changed in [dict(response_sha='a' * 64), dict(state='unknown'),
                        dict(identity_sha='b' * 64), dict(extra=1), dict(cleanup_verified=1)]:
            with self.assertRaises(RunnerProtocolError):
                validate_evidence({**base, **changed}, identity)
        failed = {**base, 'state': 'result', 'response': response,
                  'response_sha': digest(response), 'cleanup_verified': False}
        self.assertFalse(validate_evidence(failed, identity).importable)

    def test_frame_round_trip_and_exact_limits(self):
        doc = {'value': '雪'}
        size = len(json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode())
        frame = encode_frame(doc, limit=size)
        self.assertEqual(decode_frame(frame, limit=size), doc)
        self.assertEqual(read_frame(io.BytesIO(frame), limit=size), doc)
        async def check():
            reader = asyncio.StreamReader()
            reader.feed_data(frame)
            reader.feed_eof()
            return await read_frame_async(reader, limit=size)
        self.assertEqual(asyncio.run(check()), doc)
        with self.assertRaises(RunnerProtocolError):
            encode_frame(doc, limit=size - 1)
        for broken in [frame[:-1], frame + frame, struct.pack('!I', size + 1) + frame[4:],
                       struct.pack('!I', 0), b'\x00', struct.pack('!I', 2) + b'[]']:
            with self.assertRaises(RunnerProtocolError):
                decode_frame(broken, limit=size)

    def test_duplicate_keys_nonfinite_and_non_utf8_frames_rejected(self):
        for payload in [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":"\xff"}',
                        b'{"a":"\\ud800"}', b'{"a":' + b'[' * 1000 + b'0' + b']' * 1000 + b'}']:
            with self.assertRaises(RunnerProtocolError):
                decode_frame(struct.pack('!I', len(payload)) + payload, limit=8192)

    def test_metadata_limit(self):
        doc = identity_document()
        doc['research_context']['research_id'] = 'a' * 8192
        with self.assertRaises(RunnerProtocolError):
            validate_identity(doc)
