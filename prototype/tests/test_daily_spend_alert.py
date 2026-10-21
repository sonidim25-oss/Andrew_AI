import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from andrewbot.controller import Controller
from andrewbot.dispatch import ControllerDispatch
from andrewbot.main import daily_spend_alert_usd
from andrewbot.store import Store
from andrewbot.telegram import OwnerRateLimit


NOON_OCT_21 = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
NOON_OCT_22 = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)


class DailySpendAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.db'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())

    def controller(self, threshold=1.0):
        return Controller(self.store, Mock(), 17, daily_spend_alert_usd=threshold)

    def spend(self, day, cost):
        # Totals are bucketed by created_at, so date each fixture row explicitly.
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO usage(month,day,cost_usd,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?)",
                (day[:7], day, cost, 10, 10, day + 'T16:00:00+00:00'))

    def alerts(self):
        return [dict(row) for row in self.store.connection.execute('SELECT * FROM telegram_outbox ORDER BY id')]

    def test_spending_below_the_alert_level_sends_nothing(self):
        self.spend('2026-10-21', 0.99)
        self.assertFalse(self.controller().check_spend_alert(now=NOON_OCT_21))
        self.assertEqual(self.alerts(), [])

    def test_crossing_the_alert_level_queues_one_system_message_per_day(self):
        controller = self.controller()
        self.spend('2026-10-20', 4.00)
        self.spend('2026-10-21', 0.60)
        self.spend('2026-10-21', 0.65)
        self.assertTrue(controller.check_spend_alert(now=NOON_OCT_21))
        self.spend('2026-10-21', 2.00)
        self.assertFalse(controller.check_spend_alert(now=NOON_OCT_21))
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]['chat_id'], alerts[0]['lane']), (17, 'system'))
        self.assertIn('$1.25 used today (2026-10-21)', alerts[0]['text'])
        self.assertIn('$1.00 alert level', alerts[0]['text'])
        self.assertIn('This month so far: $5.25', alerts[0]['text'])
        self.assertIn('Daily spending alert sent (2026-10-21)', [row['event'] for row in self.store.recent_activity(3)])

    def test_alert_is_not_repeated_after_restart_and_resumes_the_next_day(self):
        self.spend('2026-10-21', 1.50)
        self.assertTrue(self.controller().check_spend_alert(now=NOON_OCT_21))
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.controller().check_spend_alert(now=NOON_OCT_21))
        self.assertFalse(self.controller().check_spend_alert(now=NOON_OCT_22))
        self.spend('2026-10-22', 1.00)
        self.assertTrue(self.controller().check_spend_alert(now=NOON_OCT_22))
        self.assertEqual(len(self.alerts()), 2)

    def test_disabled_alert_never_queues(self):
        self.spend('2026-10-21', 9.00)
        for threshold in (None, 0):
            self.assertFalse(self.controller(threshold).check_spend_alert(now=NOON_OCT_21))
        self.assertEqual(self.alerts(), [])


class SpendAlertConfigurationTests(unittest.TestCase):
    def configured(self, value):
        with patch.dict('os.environ', {} if value is None else {'ANDREW_DAILY_SPEND_ALERT_USD': value}, clear=False):
            if value is None:
                import os
                os.environ.pop('ANDREW_DAILY_SPEND_ALERT_USD', None)
            return daily_spend_alert_usd()

    def test_default_and_explicit_values(self):
        self.assertEqual(self.configured(None), 1.0)
        self.assertEqual(self.configured('2.5'), 2.5)
        self.assertIsNone(self.configured('0'))

    def test_invalid_values_fall_back_to_the_default(self):
        for value in ('abc', '-1', 'nan', 'inf', '5000'):
            with self.subTest(value=value), self.assertLogs(level='WARNING'):
                self.assertEqual(self.configured(value), 1.0)


class SpendAlertDispatchTests(unittest.TestCase):
    def test_dispatch_checks_at_most_once_per_minute_and_survives_errors(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name) / 'state.db')
        self.addCleanup(store.close)
        controller = Mock()
        controller.check_spend_alert.side_effect = RuntimeError('fixture')
        telegram = Mock()
        telegram.get_updates.return_value = []
        dispatch = ControllerDispatch(store, controller, telegram, Mock(), OwnerRateLimit(), 17, False, False)
        clock = [1000.0]
        with patch('andrewbot.dispatch.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(dispatch, '_finish_model_pass'), self.assertLogs(level='WARNING'):
            dispatch.step()
            clock[0] += 59
            dispatch.step()
            clock[0] += 1
            dispatch.step()
        self.assertEqual(controller.check_spend_alert.call_count, 2)


if __name__ == '__main__':
    unittest.main()
