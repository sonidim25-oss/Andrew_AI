from telegram_fixture import sent_media
from dataclasses import replace
import json
from unittest.mock import Mock, patch
import urllib.error

from test_media import MediaCase
from andrewbot.controller import Controller
from andrewbot.media import MediaWorker
from andrewbot.memory import MemoryService
from andrewbot.model import OpenRouterModel


class AutonomousMediaTests(MediaCase):
    def setUp(self):
        super().setUp()
        self.media.policy = replace(self.media.policy, spontaneous_voice=True, spontaneous_image=True)

    def test_autonomous_voice_and_image_jobs_are_idempotent_and_owner_delivered(self):
        import struct
        import zlib
        def chunk(kind, body):
            return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b''))
        provider, telegram = Mock(), Mock()
        telegram.send_media.side_effect = lambda chat, data, mime: sent_media(chat, mime, 7)
        worker = MediaWorker(self.media, provider, telegram)
        for kind, data, mime in (('voice', b'ID3' + bytes(40), 'audio/mpeg'), ('image', png, 'image/png')):
            args = {'text': 'Something I chose to share', 'request_id': kind + '-choice-1'}
            job = self.media.execute(kind + '.generate', args)
            self.assertEqual(job['job_id'], self.media.execute(kind + '.generate', args)['job_id'])
            provider.generate.return_value = (data, mime, .01)
            worker.step()
            self.assertEqual(self.media.job(job['job_id'])['delivery'], 'delivered')
            telegram.send_media.assert_called_with(42, data, mime)
        self.assertEqual(provider.generate.call_count, 2)

    def test_uncertain_autonomous_job_is_not_repeated(self):
        arguments = {'text': 'A voice thought', 'request_id': 'thought-1'}
        job = self.media.execute('voice.generate', arguments)['job_id']
        provider = Mock()
        provider.generate.side_effect = TimeoutError()
        worker = MediaWorker(self.media, provider, Mock())
        worker.step()
        self.assertEqual(self.media.job(job)['state'], 'uncertain')
        self.assertEqual(self.media.execute('voice.generate', arguments)['job_id'], job)
        worker.step()
        provider.generate.assert_called_once()

    def test_voice_switch_revokes_queued_spontaneous_voice_but_keeps_images(self):
        voice = self.media.execute('voice.generate', {'text': 'Hello', 'request_id': 'voice-1'})['job_id']
        controller = Controller(self.store, Mock(), 42, media=self.media)
        self.assertIn('off', controller._command('/voice_off'))
        with self.assertRaises(ValueError):
            self.media.execute('voice.generate', {'text': 'Again', 'request_id': 'voice-2'})
        self.assertIsNone(self.media.claim())
        self.assertEqual(self.media.job(voice)['error'], 'policy_changed')
        image = self.media.execute('image.generate', {'text': 'A tree', 'request_id': 'image-1'})
        self.assertEqual(self.media.claim()['id'], image['job_id'])
        self.assertIn('on', controller._command('/voice_on'))
        self.assertTrue(self.media.status()['spontaneous_voice'])

    def test_pause_prevents_spontaneous_generation_and_delivery(self):
        self.store.set_state('paused', '1')
        with self.assertRaises(ValueError):
            self.media.execute('voice.generate', {'text': 'Hello', 'request_id': 'paused'})
        self.assertIsNone(self.media.claim())
        self.assertIsNone(self.media.claim_delivery())

    def test_owner_credit_guidance_survives_and_enters_memory_context(self):
        self.store.set_state('owner_credit_guidance', 'Finite API credits; I decide how to allocate them.')
        self.assertIn('Finite API credits', MemoryService(self.store).current_personality_context())

    def test_budget_tool_reads_provider_metadata(self):
        reader = Mock(return_value={'available': True, 'key': {'limit_remaining': 12}})
        controller = Controller(self.store, Mock(), 42, media=self.media, credit_reader=reader)
        result = json.loads(controller.execute_tool('check_budget', {}))
        self.assertEqual(result['provider_credits']['key']['limit_remaining'], 12)
        self.assertIsNone(result['media']['voice']['daily_cap_usd'])
        reader.assert_called_once()


class CreditMetadataTests(MediaCase):
    def test_balance_credential_is_read_on_demand_and_used_only_for_account_credits(self):
        def response(data):
            result = Mock()
            result.__enter__ = Mock(return_value=result)
            result.__exit__ = Mock(return_value=False)
            result.read.return_value = json.dumps({'data': data}).encode()
            return result
        opener = Mock()
        opener.open.side_effect = [response({'usage': 4}), response({'total_credits': 25, 'total_usage': 4}),
                                   response({'usage': 5}), response({'total_credits': 25, 'total_usage': 5})]
        reader = Mock(side_effect=['first-private-credit-key', 'second-private-credit-key'])
        model = OpenRouterModel('private-inference-key', credit_key_reader=reader)
        with patch('urllib.request.build_opener', return_value=opener):
            first = model.credit_status()
            second = model.credit_status()
        self.assertEqual(first['credits']['remaining_usd'], 21)
        self.assertEqual(second['credits']['remaining_usd'], 20)
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual([request.get_header('Authorization') for request in requests],
                         ['Bearer private-inference-key', 'Bearer first-private-credit-key',
                          'Bearer private-inference-key', 'Bearer second-private-credit-key'])
        self.assertTrue(second['balance_known'])
        self.assertNotIn('private', json.dumps(second))
        self.assertEqual(reader.call_count, 2)

    def test_balance_credential_failure_does_not_expose_secret_error_text(self):
        reader = Mock(side_effect=RuntimeError('private credential details'))
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('fixed', 403, 'private provider text', {}, None)
        with patch('urllib.request.build_opener', return_value=opener):
            result = OpenRouterModel('private-inference-key', credit_key_reader=reader).credit_status()
        self.assertEqual(result['credit_credential_status'], 'unavailable')
        self.assertNotIn('private', json.dumps(result))

    def test_provider_credit_response_excludes_key_identity_and_secrets(self):
        def response(data):
            mock = Mock()
            mock.__enter__ = Mock(return_value=mock)
            mock.__exit__ = Mock(return_value=False)
            mock.read.return_value = json.dumps({'data': data}).encode()
            return mock
        opener = Mock()
        opener.open.side_effect = [response({'limit': 20, 'limit_remaining': 12, 'usage': 8,
                                             'label': 'private account', 'key': 'private key'}),
                                   response({'total_credits': 30, 'total_usage': 9})]
        with patch('urllib.request.build_opener', return_value=opener):
            result = OpenRouterModel('private-token').credit_status()
        self.assertEqual(result['credits']['remaining_usd'], 21)
        self.assertNotIn('private', json.dumps(result))

    def test_missing_management_permission_reports_balance_unknown(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('fixed', 403, 'private error', {}, None)
        with patch('urllib.request.build_opener', return_value=opener):
            result = OpenRouterModel('private-token').credit_status()
        self.assertFalse(result['available'])
        self.assertEqual(result['credits_status'], 'management_key_required')
        self.assertNotIn('private', json.dumps(result))
