"""Regressions for the owner's selected media review repairs (2, 3, 4)."""
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import Mock
import zlib

from andrewbot.dispatch import ControllerDispatch
from andrewbot.media import MediaPolicy, MediaService, MediaWorker
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit


def png_fixture():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b''))


class MediaReviewCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / 'controller.sqlite3')
        self.addCleanup(self.store.close)
        self.media = MediaService(self.store, Path(self.directory.name) / 'artifacts', 42, MediaPolicy(enabled=True))
        self.provider = Mock()
        self.provider.generate.return_value = (b'ID3' + bytes(40), 'audio/mpeg', .001)
        self.telegram = Mock()
        self.telegram.send_media.return_value = {'message_id': 10}
        self.worker = MediaWorker(self.media, self.provider, self.telegram)

    def saved_image(self, key='image'):
        job = self.media.submit('image', 'a cat', key, owner_requested=True)
        self.media.claim()
        self.media.complete(job, png_fixture(), 'image/png', .001)
        self.media.claim_delivery()
        self.media.acknowledge(job, 9)
        return job


class ShutdownReviewTests(MediaReviewCase):
    def test_dispatch_signals_all_workers_before_waiting_for_model(self):
        job = self.media.submit('voice', 'hello', 'shutdown', owner_requested=True)
        dispatch = ControllerDispatch(self.store, Mock(), self.telegram, Mock(), OwnerRateLimit(),
                                      42, False, False, media_worker=self.worker, diary_action=lambda **kw: None)
        observed = []
        def model_stop(timeout):
            observed.append((dispatch._model_worker._stop.is_set(), dispatch._diary_worker._stop.is_set(), self.worker.stopping.is_set()))
            self.worker.step()
            return True
        dispatch._model_worker.stop = model_stop
        self.assertTrue(dispatch.stop())
        self.assertEqual([(True, True, True)], observed)
        self.provider.generate.assert_not_called()
        self.telegram.send_media.assert_not_called()
        self.assertEqual('queued', self.media.job(job)['state'])

    def test_dispatch_shutdown_blocks_pending_media_delivery(self):
        job = self.media.submit('voice', 'hello', 'delivery', owner_requested=True)
        self.media.claim()
        self.media.complete(job, b'ID3' + bytes(40), 'audio/mpeg', .001)
        dispatch = ControllerDispatch(self.store, Mock(), self.telegram, Mock(), OwnerRateLimit(),
                                      42, False, False, media_worker=self.worker)
        dispatch._model_worker.stop = lambda timeout: self.worker.step() or True
        self.assertTrue(dispatch.stop())
        self.telegram.send_media.assert_not_called()
        self.assertEqual('pending', self.media.job(job)['delivery'])

    def test_dispatch_shutdown_blocks_approved_avatar_application(self):
        proposal = self.media.propose_avatar(self.saved_image())
        self.media.approve_avatar(proposal)
        dispatch = ControllerDispatch(self.store, Mock(), self.telegram, Mock(), OwnerRateLimit(),
                                      42, False, False, media_worker=self.worker)
        dispatch._model_worker.stop = lambda timeout: self.worker.step() or True
        self.assertTrue(dispatch.stop())
        self.telegram.set_profile_photo.assert_not_called()
        state = self.store.connection.execute('SELECT state FROM media_avatar_proposals WHERE id=?', (proposal,)).fetchone()[0]
        self.assertEqual('approved', state)

    def test_inflight_media_keeps_storage_open_and_saves_result_during_shutdown(self):
        from andrewbot.main import close_controller_resources
        entered, release = threading.Event(), threading.Event()
        def generate(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('blocked media fixture was not released')
            return b'ID3' + bytes(40), 'audio/mpeg', .001
        self.provider.generate.side_effect = generate
        job = self.media.submit('voice', 'hello', 'inflight', owner_requested=True)
        dispatch = ControllerDispatch(self.store, Mock(), self.telegram, Mock(), OwnerRateLimit(),
                                      42, False, False, media_worker=self.worker)
        original_stop = dispatch.stop
        dispatch.stop = lambda: original_stop(timeout=.01)
        self.worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(close_controller_resources(dispatch, Mock(), Mock(), self.store))
            self.assertEqual('running', self.media.job(job)['state'])
        finally:
            release.set()
            self.assertTrue(original_stop(timeout=2))
        self.assertEqual('succeeded', self.media.job(job)['state'])
        self.assertEqual('pending', self.media.job(job)['delivery'])
        self.telegram.send_media.assert_not_called()

class RetentionReviewTests(MediaReviewCase):
    def old_image(self):
        job = self.saved_image()
        with self.store.connection:
            self.store.connection.execute("UPDATE media_jobs SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?", (job,))
        return job

    def race_cleanup_at(self, method_name, action):
        original = getattr(self.media, method_name)
        reached, started, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def cleanup():
            try:
                if not reached.wait(1):
                    raise AssertionError('reference validation was not reached')
                started.set()
                self.media.cleanup()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        def validation(*args):
            result = original(*args)
            reached.set()
            if not started.wait(1):
                raise AssertionError('cleanup fixture did not start')
            # A correct transaction blocks cleanup until the reference exists.
            # The old unlocked check lets cleanup finish inside this window.
            finished.wait(.2)
            return result
        thread = threading.Thread(target=cleanup)
        setattr(self.media, method_name, validation)
        thread.start()
        try:
            result = action()
        finally:
            setattr(self.media, method_name, original)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        return result

    def test_edit_reference_survives_cleanup_between_validation_and_insert(self):
        original = self.old_image()
        edit = self.race_cleanup_at('artifact', lambda: self.media.submit(
            'image', 'edit', 'edit', owner_requested=True, reference_id=original))
        self.assertEqual('delivered', self.media.job(original)['delivery'])
        self.assertEqual(png_fixture(), self.media.artifact(self.media.job(original)))
        self.assertEqual('queued', self.media.job(edit)['state'])

    def test_avatar_reference_survives_cleanup_between_validation_and_insert(self):
        original = self.old_image()
        proposal = self.race_cleanup_at('job', lambda: self.media.propose_avatar(original))
        self.assertEqual('delivered', self.media.job(original)['delivery'])
        self.assertEqual(png_fixture(), self.media.artifact(self.media.job(original)))
        self.assertTrue(self.media.approve_avatar(proposal))

    def test_missing_edit_artifact_before_provider_call_releases_reservation(self):
        original = self.saved_image()
        edit = self.media.submit('image', 'edit', 'missing', owner_requested=True, reference_id=original)
        (self.media.artifacts / (original + '.png')).unlink()
        self.worker.step()
        self.assertEqual('failed', self.media.job(edit)['state'])
        self.assertEqual('preflight_failed', self.media.job(edit)['error'])
        self.assertEqual(0, self.media.status()['image']['reserved_usd'])
        self.provider.generate.assert_not_called()

    def test_cleanup_uses_database_transaction_to_protect_other_connections(self):
        other_store = Store(self.store.path)
        self.addCleanup(other_store.close)
        other_media = MediaService(other_store, self.media.artifacts, 42, self.media.policy)
        original = self.old_image()
        real_cleanup = self.media.cleanup
        self.media.cleanup = other_media.cleanup
        try:
            self.race_cleanup_at('artifact', lambda: self.media.submit(
                'image', 'edit', 'separate-connection', owner_requested=True, reference_id=original))
        finally:
            self.media.cleanup = real_cleanup
        self.assertEqual('delivered', self.media.job(original)['delivery'])

class ShutdownPreparationTests(MediaReviewCase):
    def test_stop_during_edit_preparation_does_not_start_provider(self):
        source = self.saved_image()
        edit = self.media.submit('image', 'edit', 'stop-edit', owner_requested=True, reference_id=source)
        artifact = self.media.artifact
        def read_then_stop(row):
            data = artifact(row)
            self.worker.request_stop()
            return data
        self.media.artifact = read_then_stop
        self.worker.step()
        self.provider.generate.assert_not_called()
        self.assertEqual('queued', self.media.job(edit)['state'])

    def test_stop_during_delivery_preparation_leaves_delivery_pending(self):
        job = self.media.submit('voice', 'hello', 'stop-read', owner_requested=True)
        self.media.claim()
        self.media.complete(job, b'ID3' + bytes(40), 'audio/mpeg', .001)
        artifact = self.media.artifact
        def read_then_stop(row):
            data = artifact(row)
            self.worker.request_stop()
            return data
        self.media.artifact = read_then_stop
        self.worker.step()
        self.telegram.send_media.assert_not_called()
        self.assertEqual('pending', self.media.job(job)['delivery'])

    def test_stop_during_avatar_conversion_prevents_publication(self):
        from unittest.mock import patch
        proposal = self.media.propose_avatar(self.saved_image())
        self.media.approve_avatar(proposal)
        def convert(data):
            self.worker.request_stop()
            return b'\xff\xd8' + bytes(40)
        with patch('andrewbot.media_provider.avatar_jpeg', side_effect=convert):
            self.worker.step()
        self.telegram.set_profile_photo.assert_not_called()
        state = self.store.connection.execute('SELECT state FROM media_avatar_proposals WHERE id=?', (proposal,)).fetchone()[0]
        self.assertEqual('approved', state)

class TelegramDiagnosticTests(unittest.TestCase):
    def test_media_errors_preserve_safe_categories_without_sensitive_details(self):
        from urllib.error import HTTPError, URLError
        from andrewbot.telegram import TelegramAPI, TelegramMediaError
        cases = [
            (HTTPError('https://api.telegram.org/botsecret/sendPhoto', 429, 'private prompt', None, None), 'http_error', 429),
            (URLError('secret private prompt'), 'network_error', None),
            (URLError(TimeoutError('secret private prompt')), 'timeout', None),
            (TimeoutError('secret private prompt'), 'timeout', None),
            (RuntimeError('secret private prompt'), 'unexpected_error', None),
        ]
        for error, category, status in cases:
            with self.subTest(category=category, wrapped=type(error).__name__):
                api = TelegramAPI('secret', opener=Mock(side_effect=error))
                with self.assertRaises(TelegramMediaError) as caught:
                    api.send_media(42, b'ID3' + bytes(40), 'audio/mpeg')
                self.assertEqual(category, caught.exception.category)
                self.assertEqual(status, caught.exception.http_status)
                self.assertNotIn('secret', str(caught.exception))
                self.assertNotIn('private prompt', str(caught.exception))
                self.assertNotIn('https://', str(caught.exception))

    def test_malformed_and_rejected_responses_have_safe_diagnostics(self):
        from andrewbot.telegram import TelegramAPI, TelegramMediaError
        cases = [(b'private invalid JSON', 'invalid_response', None),
                 (json.dumps({'ok': False, 'error_code': 400, 'description': 'secret private prompt'}).encode(), 'api_rejected', 400),
                 (json.dumps({'ok': True, 'result': {'message_id': 'secret'}}).encode(), 'invalid_response', None)]
        for body, category, status in cases:
            response = Mock()
            response.read.return_value = body
            context = Mock()
            context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock(return_value=False)
            api = TelegramAPI('secret', opener=Mock(return_value=context))
            with self.assertRaises(TelegramMediaError) as caught:
                api.send_media(42, b'ID3' + bytes(40), 'audio/mpeg')
            self.assertEqual(category, caught.exception.category)
            self.assertEqual(status, caught.exception.http_status)
            self.assertNotIn('secret', str(caught.exception))
            self.assertNotIn('private', str(caught.exception))


class DeliveryNoticeTests(MediaReviewCase):
    def prepared_voice(self):
        job = self.media.submit('voice', 'hello', 'notice', owner_requested=True)
        self.media.claim()
        self.media.complete(job, b'ID3' + bytes(40), 'audio/mpeg', .001)
        return job

    def notices(self):
        with self.store._lock:
            return [dict(row) for row in self.store.connection.execute('SELECT text,lane FROM telegram_outbox')]

    def test_failed_send_is_held_with_one_local_notice_and_safe_http_status(self):
        from andrewbot.telegram import TelegramMediaError
        job = self.prepared_voice()
        self.telegram.send_media.side_effect = TelegramMediaError('http_error', 429)
        self.worker.step()
        self.worker.step()
        self.media.recover()
        self.assertEqual('uncertain', self.media.job(job)['delivery'])
        self.assertEqual('delivery_http_error_429', self.media.job(job)['error'])
        self.assertEqual(1, self.telegram.send_media.call_count)
        notices = self.notices()
        self.assertEqual(1, len(notices))
        self.assertEqual('system', notices[0]['lane'])
        self.assertIn(job, notices[0]['text'])
        self.assertIn('held', notices[0]['text'])

    def test_timeout_is_held_without_persisting_exception_text(self):
        job = self.prepared_voice()
        self.telegram.send_media.side_effect = TimeoutError('secret private prompt')
        self.worker.step()
        self.assertEqual('delivery_timeout', self.media.job(job)['error'])
        self.assertEqual(1, len(self.notices()))
        self.assertNotIn('secret', str(self.media.status()) + str(self.notices()))

    def test_restart_recovery_notifies_once_without_resending(self):
        job = self.prepared_voice()
        self.media.claim_delivery()
        self.media.recover()
        self.media.recover()
        self.worker.step()
        self.assertEqual('delivery_interrupted', self.media.job(job)['error'])
        self.assertEqual('uncertain', self.media.job(job)['delivery'])
        self.assertEqual(1, len(self.notices()))
        self.telegram.send_media.assert_not_called()

    def test_notice_and_hold_are_atomic_if_local_queue_write_fails(self):
        import sqlite3
        job = self.prepared_voice()
        self.media.claim_delivery()
        with self.store.connection:
            self.store.connection.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON telegram_outbox BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.media.hold_delivery(job, TimeoutError('sensitive'))
        self.assertEqual('sending', self.media.job(job)['delivery'])
        with self.store.connection:
            self.store.connection.execute('DROP TRIGGER fail_notice')
        self.media.recover()
        self.assertEqual('uncertain', self.media.job(job)['delivery'])
        self.assertEqual(1, len(self.notices()))
