import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from andrewbot.appserver import LettaTurnError
from andrewbot.controller import Controller
from andrewbot.gateway import ModelGateway
from andrewbot.model import ProviderRequestError
from andrewbot.sessions import SessionRunner
from andrewbot.store import Store

OK = {'id': 'ok', 'choices': [{'message': {'role': 'assistant', 'content': 'done'}, 'finish_reason': 'stop'}],
      'usage': {'prompt_tokens': 5, 'completion_tokens': 1, 'cost': 0.000001}}


class Provider:
    def __init__(self):
        self.mode = 'unsent'
        self.calls = 0

    def complete(self, payload):
        self.calls += 1
        if self.mode == 'unsent':
            raise ProviderRequestError('network_error', request_sent=False)
        if self.mode == 'uncertain':
            raise TimeoutError('response lost')
        return OK


class RuntimeAgent:
    """Mirrors the measured pinned 0.34.13 behaviour: 4 outer attempts x 4 SDK tries, growing input."""

    def __init__(self, test):
        self.test = test
        self.before = None

    def autonomous_turn(self, prompt, timeout=None):
        if self.before:
            self.before()
        for outer in range(4):
            payload = {'model': self.test.gateway.model_id,
                       'messages': [{'role': 'user', 'content': prompt}] * (outer + 1)}
            for _ in range(4):
                status, result = self.test.gateway.complete('Bearer token', payload)
                if status == 200:
                    return 'done', {}
                if status == 422:
                    raise LettaTurnError(result['error'].get('code', 'turn_failed'))
                if status != 503:
                    raise LettaTurnError('turn_failed', 'turn_finished', 'error')
        raise LettaTurnError('provider_not_reached', 'turn_finished', 'llm_api_error')


class UnsentProviderRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'controller.sqlite3'
        self.provider = Provider()
        self.agent = RuntimeAgent(self)
        self.open()
        self.now = datetime(2026, 10, 21, 13, tzinfo=timezone.utc)

    def open(self):
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.gateway = ModelGateway(self.provider, self.store, 'token')
        self.controller = Controller(self.store, self.agent, 17, autonomous_enabled=True)
        self.runner = SessionRunner(self.store, self.controller, self.agent, enabled=True)

    def count(self, sql):
        return self.store.connection.execute(sql).fetchone()[0]

    def fail_first_cycle(self):
        self.controller._command('/wake')
        self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.provider.calls, 16)

    def assert_scheduled(self, delay=60):
        self.assertEqual(self.store.get_state('autonomy_error', ''), '')
        self.assertEqual(self.store.get_state('session_start_requested'), 'provider_retry')
        self.assertEqual(self.store.get_state('provider_retry_at'), (self.now + timedelta(seconds=delay)).isoformat())
        self.assertEqual(self.store.get_state('active_session_id', ''), '')
        self.assertEqual(self.count('SELECT count(*) FROM budget_reservations'), 0)

    def assert_stopped(self, category):
        self.assertEqual(self.store.get_state('autonomy_error'), category)
        self.assertNotEqual(self.store.get_state('session_start_requested', ''), 'provider_retry')

    def test_exhaustion_schedules_backoff_without_busy_loop_then_retry_succeeds(self):
        self.fail_first_cycle()
        self.assert_scheduled()
        self.assertEqual(self.count("SELECT count(*) FROM provider_attempts WHERE outcome='not_reached'"), 16)
        self.assertEqual(self.count("SELECT count(*) FROM cycles WHERE status='failed'"), 1)
        self.assertFalse(self.runner.tick(self.now + timedelta(seconds=59)))
        self.assertEqual(self.provider.calls, 16)
        self.provider.mode = 'ok'
        self.assertTrue(self.runner.tick(self.now + timedelta(seconds=60)))
        self.assertEqual(self.provider.calls, 17)
        self.assertEqual(self.count("SELECT count(*) FROM cycles WHERE status='complete'"), 1)
        self.assertEqual(self.store.get_state('provider_retry_streak', ''), '')
        self.assertEqual(self.store.get_state('autonomy_error', ''), '')

    def test_repeated_exhaustion_backs_off_then_stops_at_cap(self):
        self.fail_first_cycle()
        due = self.now
        for delay in Store.PROVIDER_RETRY_DELAYS[1:]:
            due += timedelta(seconds=Store.PROVIDER_RETRY_DELAYS[Store.PROVIDER_RETRY_DELAYS.index(delay) - 1])
            self.assertFalse(self.runner.tick(due))
            self.assertEqual(self.store.get_state('provider_retry_at'), (due + timedelta(seconds=delay)).isoformat())
        self.assertFalse(self.runner.tick(due + timedelta(seconds=Store.PROVIDER_RETRY_DELAYS[-1])))
        self.assert_stopped('provider_not_reached')

    def test_stale_proof_does_not_excuse_a_later_uncertain_failure(self):
        self.fail_first_cycle()
        self.provider.mode = 'uncertain'
        self.assertFalse(self.runner.tick(self.now + timedelta(seconds=60)))
        self.assert_stopped('provider_attempt_uncertain')
        self.assertEqual(self.count('SELECT count(*) FROM budget_reservations'), 1)

    def test_typed_hint_without_matching_attempt_proof_keeps_stop(self):
        self.fail_first_cycle()
        self.provider.mode = 'uncertain'
        def forged():
            self.gateway.complete('Bearer token', {'model': self.gateway.model_id, 'messages': [{'role': 'user', 'content': 'x'}]})
            raise LettaTurnError('provider_not_reached', 'turn_finished', 'llm_api_error')
        self.agent.autonomous_turn = lambda prompt, timeout=None: forged()
        self.assertFalse(self.runner.tick(self.now + timedelta(seconds=60)))
        self.assert_stopped('provider_not_reached')

    def test_turn_without_any_recorded_attempt_keeps_stop(self):
        def no_dispatch(prompt, timeout=None):
            raise LettaTurnError('provider_not_reached', 'turn_finished', 'llm_api_error')
        self.agent.autonomous_turn = no_dispatch
        self.controller._command('/wake')
        self.assertFalse(self.runner.tick(self.now))
        self.assert_stopped('provider_not_reached')

    def test_completed_provider_step_or_executed_tool_keeps_stop(self):
        for prior in ('provider', 'tool'):
            with self.subTest(prior=prior):
                self.store.close()
                self.path.unlink()
                for suffix in ('-wal', '-shm'):
                    Path(str(self.path) + suffix).unlink(missing_ok=True)
                self.open()
                self.provider.mode, self.provider.calls = 'unsent', 0
                def side_effect():
                    if prior == 'provider':
                        self.provider.mode = 'ok'
                        self.gateway.complete('Bearer token', {'model': self.gateway.model_id,
                                                               'messages': [{'role': 'user', 'content': 'step'}]})
                        self.provider.mode = 'unsent'
                    else:
                        cycle = self.store.get_state('active_cycle_id')
                        attempt = self.store.begin_cycle_tool((self.runner.active_session_id, cycle, None), 'research.save')
                        self.store.finish_cycle_tool(attempt, True)
                self.agent.before = side_effect
                self.controller._command('/wake')
                self.assertFalse(self.runner.tick(self.now))
                self.assert_stopped('provider_not_reached')
        self.agent.before = None

    def test_held_owner_message_keeps_stop(self):
        held = self.store.add_message(17, 'user', 'held', queued=True)
        self.store.claim_owner_message(17, held)
        self.store.hold_uncertain_queued_claim([held])
        self.store.set_state('autonomy_error', '')
        self.fail_first_cycle()
        self.assert_stopped('provider_not_reached')

    def test_owner_revision_change_during_backoff_cancels_retry(self):
        self.fail_first_cycle()
        self.store.transition_owner_control({}, 'fixture owner control')
        self.provider.mode = 'ok'
        self.assertFalse(self.runner.tick(self.now + timedelta(seconds=60)))
        self.assertEqual(self.provider.calls, 16)
        self.assertEqual(self.store.get_state('session_start_requested', ''), '')
        self.assertEqual(self.store.get_state('autonomy_error', ''), '')

    def test_pause_during_backoff_cancels_retry(self):
        self.fail_first_cycle()
        self.controller._command('/pause')
        self.provider.mode = 'ok'
        self.assertFalse(self.runner.tick(self.now + timedelta(seconds=60)))
        self.assertEqual(self.provider.calls, 16)
        self.assertEqual(self.store.get_state('session_start_requested', ''), '')

    def test_owner_input_waits_without_cancelling_the_retry(self):
        self.fail_first_cycle()
        self.store.add_message(17, 'user', 'question', queued=True)
        self.assertFalse(self.runner.tick(self.now + timedelta(seconds=60)))
        self.assertEqual(self.provider.calls, 16)
        self.assertEqual(self.store.get_state('session_start_requested'), 'provider_retry')

    def test_restore_fence_cancels_retry(self):
        self.fail_first_cycle()
        self.store.set_state('local_recovery_restore_required', '1')
        self.provider.mode = 'ok'
        self.assertFalse(self.runner.tick(self.now + timedelta(seconds=60)))
        self.assertEqual(self.provider.calls, 16)
        self.assertEqual(self.store.get_state('session_start_requested', ''), '')

    def test_backoff_survives_restart(self):
        self.fail_first_cycle()
        self.store.close()
        self.open()
        self.runner.recover()
        self.assert_scheduled()
        self.provider.mode = 'ok'
        self.assertTrue(self.runner.tick(self.now + timedelta(seconds=60)))
        self.assertEqual(self.provider.calls, 17)

    def test_journaled_recovery_mode_keeps_existing_stop(self):
        class Recovery:
            enabled = True
            def has_pending(self):
                return False
        self.agent.local_recovery = Recovery()
        self.fail_first_cycle()
        self.assert_stopped('provider_not_reached')


if __name__ == '__main__':
    unittest.main()
