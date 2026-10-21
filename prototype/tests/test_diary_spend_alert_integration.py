from datetime import timedelta
import unittest
from unittest.mock import Mock

from andrewbot.controller import Controller
from andrewbot.telegram import TelegramNotSent
from prototype.tests import test_diary_unsent_retry as fixture


class DiarySpendAlertIntegrationTests(unittest.TestCase):
    setUp = fixture.DiaryUnsentRetryTests.setUp
    send = fixture.DiaryUnsentRetryTests.send
    reopen = fixture.DiaryUnsentRetryTests.reopen

    def test_diary_retry_and_restart_preserve_one_daily_spending_alert(self):
        with self.store.connection:
            self.store.connection.execute(
                'INSERT INTO usage(month,day,cost_usd,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?)',
                ('2026-10', '2026-10-21', 1.25, 10, 10, self.now.isoformat()))
        controller = Controller(self.store, Mock(), 17, daily_spend_alert_usd=1)
        self.assertTrue(controller.check_spend_alert(now=self.now))
        self.failures[1] = TelegramNotSent('fixture not sent')
        self.assertEqual(self.send(), 0)
        self.reopen()
        self.now += timedelta(seconds=1)
        self.assertEqual(self.send(), 1)
        controller = Controller(self.store, Mock(), 17, daily_spend_alert_usd=1)
        self.assertFalse(controller.check_spend_alert(now=self.now))
        alerts = self.store.connection.execute('SELECT chat_id,lane,text FROM telegram_outbox').fetchall()
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]['chat_id'], alerts[0]['lane']), (17, 'system'))
        self.assertIn('$1.25', alerts[0]['text'])
        self.assertEqual(self.store.get_state('spend_alert_day'), '2026-10-21')
