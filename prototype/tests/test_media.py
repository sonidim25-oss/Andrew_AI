from telegram_fixture import sent_media
import tempfile
import unittest
from pathlib import Path
from andrewbot.store import Store
from andrewbot.media import MediaService, MediaPolicy


class MediaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'controller.sqlite3')
        self.media = MediaService(self.store, Path(self.tmp.name) / 'media', 42,
                                  MediaPolicy(enabled=True))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class MediaTests(MediaCase):
    def test_disabled_and_recipient_policy(self):
        disabled = MediaService(self.store, Path(self.tmp.name) / 'media', 42, MediaPolicy())
        with self.assertRaises(ValueError):
            disabled.submit('voice', 'hello', 'a', owner_requested=True)
        with self.assertRaises(ValueError):
            self.media.submit('voice', 'hello', 'a', owner_requested=False)

    def test_idempotency_budget_and_restart_hold(self):
        first = self.media.submit('voice', 'hello', 'a', owner_requested=True)
        self.assertEqual(first, self.media.submit('voice', 'hello', 'a', owner_requested=True))
        claimed = self.media.claim()
        self.assertEqual(first, claimed['id'])
        self.media.recover()
        self.assertEqual('uncertain', self.media.job(first)['state'])
        self.assertIsNone(self.media.claim())
        self.assertGreater(self.media.status()['voice']['reserved_usd'], 0)

    def test_cancel_releases_queued_reservation(self):
        job = self.media.submit('image', 'a cat', 'a', owner_requested=True)
        self.assertTrue(self.media.cancel(job))
        self.assertEqual(0, self.media.status()['image']['reserved_usd'])

    def test_media_has_no_daily_spending_cap_and_keeps_separate_accounting(self):
        for i in range(2):
            self.media.submit('image', 'a cat', str(i), owner_requested=True)
        self.media.submit('image', 'a cat', 'over', owner_requested=True)
        self.assertAlmostEqual(.3, self.media.status()['image']['reserved_usd'])
        self.assertEqual(0, self.store.connection.execute('SELECT count(*) FROM usage').fetchone()[0])

    def test_delivery_attempt_is_not_replayed_after_restart(self):
        job = self.media.submit('voice', 'hello', 'a', owner_requested=True)
        self.media.claim()
        self.media.complete(job, b'ID3' + bytes(40), 'audio/mpeg', .001)
        self.assertEqual(job, self.media.claim_delivery()['id'])
        self.media.recover()
        self.assertIsNone(self.media.claim_delivery())
        self.assertEqual('uncertain', self.media.job(job)['delivery'])

    def test_argument_mismatch_cannot_reuse_request_key(self):
        self.media.submit('voice', 'hello', 'a', owner_requested=True)
        with self.assertRaises(ValueError):
            self.media.submit('voice', 'different', 'a', owner_requested=True)

class ProviderTests(unittest.TestCase):
    def test_speech_endpoint_and_secret_safe_errors(self):
        from andrewbot.media_provider import OpenRouterMedia
        calls = []
        class Response:
            headers = {'Content-Type': 'audio/mpeg'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'ID3' + bytes(40)
        def opener(request, timeout):
            calls.append((request, timeout))
            return Response()
        provider = OpenRouterMedia('secret', opener=opener)
        data, mime, cost = provider.generate('voice', {'model': 'elevenlabs/eleven-v3', 'text': 'hi', 'voice': 'george'})
        self.assertEqual('audio/mpeg', mime)
        self.assertEqual('https://openrouter.ai/api/v1/audio/speech', calls[0][0].full_url)
        self.assertEqual(120, calls[0][1])
        self.assertIsNone(cost)

    def test_telegram_upload_uses_bytes_and_owner_chat(self):
        import json
        from andrewbot.telegram import TelegramAPI
        calls = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return json.dumps({'ok': True, 'result': sent_media(42, 'audio/mpeg', 7)}).encode()
        def opener(request, timeout):
            calls.append(request)
            return Response()
        api = TelegramAPI('secret', opener=opener)
        self.assertEqual(7, api.send_media(42, b'ID3' + bytes(40), 'audio/mpeg')['message_id'])
        self.assertTrue(calls[0].full_url.endswith('/sendVoice'))
        self.assertIn(b'42', calls[0].data)
        self.assertIn(b'ID3', calls[0].data)

class ControllerMediaTests(MediaCase):
    def test_commands_are_local_and_cannot_be_called_by_agent(self):
        from andrewbot.controller import Controller
        controller = Controller(self.store, object(), 42, media=self.media)
        update = {'update_id': 123, 'message': {'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}, 'text': '/voice Hello'}}
        answer = controller.handle_update(update)
        self.assertIn('queued', answer)
        self.assertEqual(1, len(self.media.status()['jobs']))
        controller.handle_update(update)
        self.assertEqual(1, len(self.media.status()['jobs']))
        self.assertIn('voice', controller._command('/media_budget'))
        with self.assertRaises(ValueError):
            controller.execute_tool('voice.generate', {'text': 'not authorized', 'owner_request_id': 'other'})

    def test_worker_sends_one_time_and_recovers_generation_failure(self):
        from andrewbot.media import MediaWorker
        class Provider:
            def generate(self, kind, payload): return b'ID3' + bytes(40), 'audio/mpeg', .0001
        class Telegram:
            def __init__(self): self.calls = 0
            def send_media(self, *args):
                self.calls += 1
                return sent_media(42, 'audio/mpeg', 19)
        telegram = Telegram()
        worker = MediaWorker(self.media, Provider(), telegram)
        self.media.submit('voice', 'hello', 'a', owner_requested=True)
        worker.step()
        worker.step()
        self.assertEqual(1, telegram.calls)
        self.assertEqual('delivered', self.media.status()['jobs'][0]['delivery'])

class AvatarTests(MediaCase):
    def test_avatar_cannot_apply_before_owner_approval(self):
        import struct
        job = self.media.submit('image', 'avatar', 'avatar1', owner_requested=True)
        self.media.claim()
        # A generated PNG fixture, including real chunks and compressed pixels.
        import zlib
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        image = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b'')
        self.media.complete(job, image, 'image/png', .01)
        with self.assertRaises(ValueError):
            self.media.propose_avatar(job)
        delivery = self.media.claim_delivery()
        self.media.save_delivery_receipt(delivery, sent_media(42, delivery['mime'], 9))
        self.media.acknowledge(job, 9)
        proposal = self.media.propose_avatar(job)
        self.assertIsNone(self.media.claim_avatar())
        with self.assertRaises(ValueError):
            self.media.execute('profile.apply_avatar', {'proposal_id': proposal})
        self.assertTrue(self.media.approve_avatar(proposal))
        self.assertEqual(proposal, self.media.claim_avatar()['id'])
        self.media.recover()
        self.assertIsNone(self.media.claim_avatar())

    def test_running_budget_reservation_survives_cancel(self):
        job = self.media.submit('image', 'avatar', 'avatar2', owner_requested=True)
        self.media.claim()
        self.assertFalse(self.media.cancel(job))
        self.assertEqual(.1, self.media.status()['image']['reserved_usd'])

class ConfigurationTests(unittest.TestCase):
    def test_enabled_requires_live_verification_flag(self):
        from andrewbot.media import load_media_policy
        self.assertFalse(load_media_policy({}).enabled)
        with self.assertRaises(ValueError):
            load_media_policy({'ANDREW_MEDIA_ENABLED': '1'})
        self.assertTrue(load_media_policy({'ANDREW_MEDIA_ENABLED': '1', 'ANDREW_MEDIA_CAPABILITIES_VERIFIED': '1'}).enabled)

    def test_shutdown_accounts_for_media_worker(self):
        from andrewbot.dispatch import ControllerDispatch
        from unittest.mock import Mock
        store, controller = Mock(), Mock()
        media_worker = Mock()
        media_worker.stop.return_value = False
        dispatch = ControllerDispatch(store, controller, Mock(), Mock(), Mock(), 42, False, False,
                                      media_worker=media_worker)
        self.assertFalse(dispatch.stop())
        media_worker.stop.assert_called_once()

class ResilienceTests(MediaCase):
    def test_no_daily_job_quota_but_payload_size_is_bounded(self):
        for index in range(20):
            self.media.submit('voice', 'a', 'q' + str(index), owner_requested=True)
        self.media.submit('voice', 'a', 'overquota', owner_requested=True)
        with self.assertRaises(ValueError):
            self.media.submit('voice', 'a' * 1001, 'big', owner_requested=True)

    def test_corrupt_image_is_rejected_before_delivery(self):
        import struct
        job = self.media.submit('image', 'cat', 'corrupt', owner_requested=True)
        self.media.claim()
        with self.assertRaises(ValueError):
            self.media.complete(job, b'\x89PNG\r\n\x1a\n' + b'garbage!' + struct.pack('>II', 1, 1) + bytes(40), 'image/png')

    def test_provider_failure_does_not_retry_and_notifies(self):
        from andrewbot.media import MediaWorker
        class Provider:
            calls = 0
            def generate(self, *args):
                self.calls += 1
                raise TimeoutError('sensitive provider text')
        provider = Provider()
        worker = MediaWorker(self.media, provider, object())
        job = self.media.submit('voice', 'hello', 'fail', owner_requested=True)
        worker.step()
        worker.step()
        self.assertEqual(1, provider.calls)
        self.assertEqual('uncertain', self.media.job(job)['state'])
        self.assertNotIn('sensitive', str(self.media.status()))

    def test_image_request_matches_catalog_supported_fields(self):
        import base64, json
        from andrewbot.media_provider import OpenRouterMedia
        calls = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return json.dumps({'data': [{'b64_json': base64.b64encode(b'fake image').decode(), 'media_type': 'image/png'}], 'usage': {'cost': .01}}).encode()
        def opener(request, timeout):
            calls.append(json.loads(request.data))
            return Response()
        provider = OpenRouterMedia('key', opener)
        provider.generate('image', {'model': 'openai/gpt-image-1-mini', 'text': 'cat'})
        self.assertNotIn('output_format', calls[0])
        self.assertNotIn('size', calls[0])
        self.assertEqual('1:1', calls[0]['aspect_ratio'])

class ConcurrencyTests(MediaCase):
    def test_stopped_worker_never_claims_or_calls_provider(self):
        from andrewbot.media import MediaWorker
        from unittest.mock import Mock
        provider = Mock()
        worker = MediaWorker(self.media, provider, Mock())
        job = self.media.submit('voice', 'hello', 'stop', owner_requested=True)
        worker.stopping.set()
        worker.step()
        provider.generate.assert_not_called()
        self.assertEqual('queued', self.media.job(job)['state'])

    def test_local_status_remains_responsive_during_provider_call(self):
        import threading, time
        from andrewbot.controller import Controller
        from andrewbot.media import MediaWorker
        entered, release = threading.Event(), threading.Event()
        class Provider:
            def generate(self, *args):
                entered.set()
                release.wait(3)
                return b'ID3' + bytes(40), 'audio/mpeg', .001
        class Telegram:
            def send_media(self, *args): return {'message_id': 1}
        controller = Controller(self.store, object(), 42, media=self.media)
        worker = MediaWorker(self.media, Provider(), Telegram())
        self.media.submit('voice', 'hello', 'slow', owner_requested=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            start = time.monotonic()
            answer = controller.handle_update({'update_id': 4, 'message': {'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}, 'text': '/media_status'}})
            self.assertIn('running', answer)
            self.assertLess(time.monotonic() - start, .5)
        finally:
            release.set()
            self.assertTrue(worker.stop())

    def test_tool_names_require_media_enablement(self):
        from andrewbot.appserver import AppServerClient
        client = object.__new__(AppServerClient)
        self.assertNotIn('voice.generate', client.tool_names)
        client.media_tools_enabled = True
        self.assertIn('voice.generate', client.tool_names)
        self.assertNotIn('profile.apply_avatar', client.tool_names)

    def test_concurrent_admission_records_every_reservation_without_a_spending_cap(self):
        import concurrent.futures
        other_store = Store(self.store.path)
        other = MediaService(other_store, self.media.artifacts, 42, self.media.policy)
        def submit(pair):
            media, key = pair
            try:
                return media.submit('image', 'cat', key, owner_requested=True)
            except ValueError:
                return None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(submit, [(self.media, 'one'), (other, 'two'), (self.media, 'three')]))
            self.assertEqual(3, sum(value is not None for value in results))
            self.assertAlmostEqual(.3, self.media.status()['image']['reserved_usd'])
        finally:
            other_store.close()

class EditingTests(MediaCase):
    def test_edit_requires_existing_delivered_owner_artifact(self):
        with self.assertRaises(ValueError):
            self.media.submit('image', 'edit', 'edit1', owner_requested=True, reference_id='../../etc/passwd')

    def test_image_edit_provider_accepts_inline_reference_only(self):
        import base64, json
        from andrewbot.media_provider import OpenRouterMedia
        calls = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return json.dumps({'data': [{'b64_json': base64.b64encode(b'fake').decode()}]}).encode()
        def opener(request, timeout):
            calls.append(json.loads(request.data))
            return Response()
        OpenRouterMedia('key', opener).generate('image', {'model': 'openai/gpt-image-1-mini', 'text': 'edit', 'reference_png': b'png'})
        self.assertTrue(calls[0]['input_references'][0]['image_url']['url'].startswith('data:image/png;base64,'))

class SpokenReplyTests(MediaCase):
    def test_explicit_owner_reply_grants_one_media_generation(self):
        from andrewbot.controller import Controller
        class Model:
            def reply(inner, messages):
                request_id = 'owner-reply:1'
                result = self.media.execute('voice.generate', {'text': 'My answer', 'owner_request_id': request_id})
                self.assertIn('job_id', result)
                with self.assertRaises(ValueError):
                    self.media.execute('image.generate', {'text': 'cat', 'owner_request_id': request_id})
                return 'Voice queued.', {'prompt_tokens': 1, 'completion_tokens': 1}
        controller = Controller(self.store, Model(), 42, media=self.media)
        answer = controller.handle_update({'update_id': 22, 'message': {'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}, 'text': '/voice_reply Tell me something'}})
        self.assertEqual('Voice queued.', answer)
        self.assertEqual(1, len(self.media.status()['jobs']))
        with self.assertRaises(ValueError):
            self.media.execute('voice.generate', {'text': 'another', 'owner_request_id': 'owner-reply:1'})

class RoutingTests(MediaCase):
    def test_voice_reply_backlog_does_not_block_local_owner_controls(self):
        from andrewbot.dispatch import ControllerDispatch
        from andrewbot.telegram import OwnerRateLimit
        from unittest.mock import Mock
        def update(number, text):
            return {'update_id': number, 'message': {'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}, 'text': text}}
        for number in range(1, 55):
            self.store.enqueue_update(update(number, '/voice_reply' + ('\n' if number % 2 else '\t') + 'slow work'))
        self.store.enqueue_update(update(55, '/media_status'))
        controller = Mock()
        controller.handle_update.return_value = 'status'
        dispatch = ControllerDispatch(self.store, controller, Mock(), Mock(), OwnerRateLimit(), 42, False, False)
        dispatch.process_updates(controls_only=True)
        controller.handle_update.assert_called_once()
        self.assertEqual(55, controller.handle_update.call_args.args[0]['update_id'])

class StopAdmissionTests(MediaCase):
    def test_stop_during_claim_prevents_provider_side_effect(self):
        from andrewbot.media import MediaWorker
        from unittest.mock import Mock
        worker = MediaWorker(self.media, Mock(), Mock())
        job = self.media.submit('voice', 'hello', 'stopclaim', owner_requested=True)
        original = self.media.claim
        def claim():
            result = original()
            worker.stopping.set()
            return result
        self.media.claim = claim
        worker.step()
        worker.provider.generate.assert_not_called()
        self.assertEqual('queued', self.media.job(job)['state'])

class PeriodTests(MediaCase):
    def test_stale_unstarted_authorization_cannot_spend_in_a_new_budget_period(self):
        job = self.media.submit('voice', 'hello', 'old', owner_requested=True)
        with self.store.connection:
            self.store.connection.execute("UPDATE media_jobs SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (job,))
        self.assertIsNone(self.media.claim())
        self.assertEqual('cancelled', self.media.job(job)['state'])
        self.assertEqual('authorization_expired', self.media.job(job)['error'])
