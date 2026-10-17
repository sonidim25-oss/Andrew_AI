"""Queued authorization must respect the policy in force before provider calls."""
from dataclasses import replace
from unittest.mock import Mock
from test_media import MediaCase
from andrewbot.media import MediaWorker, load_media_policy


class BudgetRevalidationTests(MediaCase):
    def test_lowered_daily_monthly_and_job_caps_block_existing_queue(self):
        for change in ({'daily_usd': .05}, {'monthly_usd': .05}, {'image_job_usd': .05}):
            with self.subTest(change=change):
                self.media.policy = replace(self.media.policy, daily_usd=.20, monthly_usd=3, image_job_usd=.10)
                job = self.media.submit('image', 'a cat', 'cap-' + next(iter(change)), owner_requested=True)
                self.media.policy = replace(self.media.policy, **change)
                provider = Mock()
                MediaWorker(self.media, provider, Mock()).step()
                provider.generate.assert_not_called()
                self.assertEqual('cancelled', self.media.job(job)['state'])
                self.assertEqual('policy_changed', self.media.job(job)['error'])
        self.assertEqual(0, self.media.status()['image']['reserved_usd'])
        self.assertEqual(3, self.store.connection.execute('SELECT count(*) FROM telegram_outbox').fetchone()[0])

    def test_queue_reservations_count_together_before_claim(self):
        first = self.media.submit('image', 'one', 'one', owner_requested=True)
        second = self.media.submit('image', 'two', 'two', owner_requested=True)
        self.media.policy = replace(self.media.policy, daily_usd=.15)
        self.assertIsNone(self.media.claim())
        self.assertEqual('cancelled', self.media.job(first)['state'])
        self.assertEqual(second, self.media.claim()['id'])

    def test_voice_rate_increase_cannot_exceed_saved_reservation(self):
        job = self.media.submit('voice', 'hello', 'rate', owner_requested=True)
        self.media.policy = replace(self.media.policy, voice_character_usd=.001)
        self.assertIsNone(self.media.claim())
        self.assertEqual('cancelled', self.media.job(job)['state'])

    def test_image_enablement_does_not_require_enabling_voice(self):
        policy = load_media_policy({'ANDREW_MEDIA_ENABLED': '1', 'ANDREW_MEDIA_CAPABILITIES_VERIFIED': '1',
                                    'ANDREW_MEDIA_VOICE_ENABLED': '0'})
        self.assertFalse(policy.voice_enabled)
        voice = self.media.submit('voice', 'hello', 'voice', owner_requested=True)
        self.media.policy = policy
        with self.assertRaises(ValueError):
            self.media.submit('voice', 'hello again', 'blocked', owner_requested=True)
        self.assertIsNone(self.media.claim())
        self.assertEqual('cancelled', self.media.job(voice)['state'])
        job = self.media.submit('image', 'cat', 'allowed', owner_requested=True)
        self.assertEqual(job, self.media.claim()['id'])
        status = self.media.status()
        self.assertTrue(status['image']['enabled'])
        self.assertFalse(status['voice']['enabled'])
