import copy
import json
import unittest
from unittest.mock import patch

from andrewbot.configuration import ModelConfig
from andrewbot.model import OpenRouterModel, ProviderRequestError


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.data).encode()


class ProviderToolNameTests(unittest.TestCase):
    def exchange(self, payload, respond=None, model_id='openai/gpt-6-luna'):
        wire = []

        def send(request, timeout):
            sent = json.loads(request.data)
            wire.append(sent)
            return Response(respond(sent) if respond else {'choices': []})

        with patch('urllib.request.OpenerDirector.open', side_effect=send):
            result = OpenRouterModel('private-key', model_config=ModelConfig(model_id, 1, 1)).complete(payload)
        return wire[0], result

    def fixture(self):
        return {'tools': [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
                          for name in ('python.run', 'python_run', 'browser.open')],
                'messages': [{'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'call1', 'type': 'function', 'function': {'name': 'python.run', 'arguments': '{"secret": "private"}'}}]},
                    {'role': 'tool', 'tool_call_id': 'call1', 'name': 'python.run', 'content': 'private result'},
                    {'role': 'assistant', 'tool_calls': [{'id': 'old', 'type': 'function', 'function': {'name': 'old.tool', 'arguments': '{}'}}]}],
                'tool_choice': {'type': 'function', 'function': {'name': 'python.run'}}}

    def test_names_are_translated_only_at_wire_boundary_and_restored(self):
        payload = self.fixture()
        original = copy.deepcopy(payload)
        def respond(sent):
            return {'model': 'provider-real-id', 'choices': [{'message': {'tool_calls': [
                {'id': 'new', 'type': 'function', 'function': {'name': sent['tools'][0]['function']['name'], 'arguments': '{}'}}]}}]}
        wire, result = self.exchange(payload, respond)
        alias = wire['tools'][0]['function']['name']
        self.assertRegex(alias, r'^[a-zA-Z0-9_-]{1,64}$')
        self.assertNotEqual(alias, 'python_run')
        self.assertEqual(wire['tools'][1], payload['tools'][1])
        self.assertEqual(wire['messages'][0]['tool_calls'][0]['function']['name'], alias)
        self.assertEqual(wire['messages'][1]['name'], alias)
        self.assertRegex(wire['messages'][2]['tool_calls'][0]['function']['name'], r'^[a-zA-Z0-9_-]{1,64}$')
        self.assertEqual(wire['tool_choice']['function']['name'], alias)
        self.assertEqual(result['choices'][0]['message']['tool_calls'][0]['function']['name'], 'python.run')
        self.assertEqual(result['model'], 'provider-real-id')
        self.assertEqual(payload, original)
        expected = copy.deepcopy(original)
        expected['model'] = 'openai/gpt-6-luna'
        expected['stream'] = False
        expected['tools'][0]['function']['name'] = alias
        expected['tools'][2]['function']['name'] = wire['tools'][2]['function']['name']
        expected['messages'][0]['tool_calls'][0]['function']['name'] = alias
        expected['messages'][1]['name'] = alias
        expected['messages'][2]['tool_calls'][0]['function']['name'] = wire['messages'][2]['tool_calls'][0]['function']['name']
        expected['tool_choice']['function']['name'] = alias
        self.assertEqual(wire, expected)

    def test_determinism_and_collision_with_valid_canonical_alias(self):
        payload = self.fixture()
        first, _ = self.exchange(payload)
        alias = first['tools'][0]['function']['name']
        payload['tools'].append({'type': 'function', 'function': {'name': alias}})
        second, _ = self.exchange(payload)
        third, _ = self.exchange(payload)
        self.assertEqual(second, third)
        names = [tool['function']['name'] for tool in second['tools']]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[-1], alias)
        self.assertNotEqual(names[0], alias)
        history_only, _ = self.exchange({'messages': payload['messages']})
        self.assertEqual(history_only['messages'][0]['tool_calls'][0]['function']['name'], first['tools'][0]['function']['name'])

    def test_collision_reserves_names_from_history_and_choice(self):
        initial, _ = self.exchange(self.fixture())
        reserved = initial['tools'][0]['function']['name']
        for source in ('history', 'choice'):
            with self.subTest(source=source):
                payload = self.fixture()
                if source == 'history':
                    payload['messages'].append({'role': 'assistant', 'tool_calls': [
                        {'id': 'valid', 'type': 'function', 'function': {'name': reserved, 'arguments': '{}'}}]})
                else:
                    payload['tool_choice']['function']['name'] = reserved
                def respond(_sent):
                    return {'choices': [{'message': {'tool_calls': [{'function': {'name': reserved, 'arguments': '{}'}}]}}]}
                wire, result = self.exchange(payload, respond)
                self.assertNotEqual(wire['tools'][0]['function']['name'], reserved)
                self.assertEqual(result['choices'][0]['message']['tool_calls'][0]['function']['name'], reserved)

    def test_long_unicode_and_punctuation_names_remain_distinct(self):
        names = ['x' * 65, 'é.run', 'a.b', 'a_b', 'a-b', 'a/b']
        wire, _ = self.exchange({'tools': [{'type': 'function', 'function': {'name': name}} for name in names]})
        aliases = [tool['function']['name'] for tool in wire['tools']]
        self.assertEqual(len(set(aliases)), len(names))
        for alias in aliases:
            self.assertRegex(alias, r'^[a-zA-Z0-9_-]{1,64}$')
        self.assertEqual(aliases[3:5], ['a_b', 'a-b'])

    def test_unknown_alias_fails_closed_without_private_details(self):
        def respond(sent):
            name = sent['tools'][0]['function']['name']
            return {'choices': [{'message': {'tool_calls': [{'function': {'name': name[:-1] + ('0' if name[-1] != '0' else '1'), 'arguments': 'private arguments'}}]}}]}
        with self.assertRaises(ProviderRequestError) as caught:
            self.exchange(self.fixture(), respond)
        self.assertEqual(caught.exception.safe_category, 'invalid_response')
        self.assertNotIn('private', str(caught.exception))

    def test_unknown_canonical_name_is_left_for_gateway_validation(self):
        response = {'choices': [{'message': {'tool_calls': [{'function': {'name': 'unknown.tool', 'arguments': '{}'}}]}}]}
        _, result = self.exchange(self.fixture(), lambda _: response)
        self.assertEqual(result, response)

    def test_qwen_is_unchanged(self):
        payload = self.fixture()
        wire, _ = self.exchange(payload, model_id='qwen/qwen3.8-flash')
        self.assertEqual(wire['tools'], payload['tools'])
        self.assertEqual(wire['messages'], payload['messages'])
        self.assertEqual(wire['tool_choice'], payload['tool_choice'])
