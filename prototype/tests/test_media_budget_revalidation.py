"""Queued authorization must respect the policy in force before provider calls."""
from dataclasses import replace
from unittest.mock import Mock
from test_media import MediaCase
from andrewbot.media import MediaWorker, load_media_policy


class BudgetRevalidationTests(MediaCase):
    def test_legacy_daily_monthly_and_job_amounts_do_not_restrict_queue(self):
        for change in ({'daily_usd': .0001}, {'monthly_usd': .0001}, {'image_job_usd': .0001}):
            with self.subTest(change=change):
                self.media.policy = replace(self.media.policy, daily_usd=.20, monthly_usd=3, image_job_usd=.10)
                job = self.media.submit('image', 'a cat', 'cap-' + next(iter(change)), owner_requested=True)
                self.media.policy = replace(self.media.policy, **change)
                self.assertEqual(job, self.media.claim()['id'])
                self.media.fail(job, uncertain=False)

    def test_reservations_are_accounting_not_an_admission_limit(self):
        jobs = [self.media.submit('image', str(index), 'image-' + str(index), owner_requested=True)
                for index in range(35)]
        self.assertEqual(len(jobs), 35)
        self.assertAlmostEqual(self.media.status()['image']['reserved_usd'], 3.5)
        self.assertIsNone(self.media.status()['image']['monthly_cap_usd'])

    def test_voice_estimate_changes_do_not_cancel_saved_work(self):
        job = self.media.submit('voice', 'hello', 'rate', owner_requested=True)
        self.media.policy = replace(self.media.policy, voice_character_usd=.001)
        self.assertEqual(job, self.media.claim()['id'])

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
