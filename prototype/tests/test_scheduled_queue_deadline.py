"""Scheduled-wake delivery uses the real turn loop with a virtual clock."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from andrewbot.appserver import AppServerClient, LettaAgent
from andrewbot.controller import Controller
from andrewbot.store import Store


class ScheduledQueueDeadlineTests(unittest.TestCase):
    def test_saved_messages_finish_after_two_minutes_without_replaying_held_input(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'controller.sqlite3')
            self.addCleanup(store.close)
            clock = [1000.0]
            sent = []
            connection = Mock()
            connection.send_json.side_effect = sent.append
            connection.recv_json.side_effect = [
                {'type': 'external_tool_call_request', 'request_id': 'tool-1',
                 'tool_name': 'shell.run', 'input': {'command': 'fixture'}},
                {'type': 'stream_delta', 'delta': {'message_type': 'assistant_message', 'id': 'answer'}},
                {'type': 'turn_finished', 'stop_reason': 'end_turn'},
            ]
            client = AppServerClient.__new__(AppServerClient)
            client.connection = connection
            client.start_runtime = Mock(return_value={'agent_id': 'fixture-agent', 'conversation_id': 'fixture-conv'})
            client.request = Mock(return_value={'messages': [
                {'message_type': 'assistant_message', 'id': 'answer', 'content': 'Finished saved work.'}]})
            agent = LettaAgent(store, 'fixture-token')
            agent.agent_id = 'fixture-agent'
            tools = []

            def finish_tool(name, arguments):
                tools.append(name)
                clock[0] += 131
                return 'fixture tool completed'

            agent.tool_handler = finish_tool
            controller = Controller(store, agent, 17, autonomous_enabled=True)
            held = store.add_message(17, 'user', 'Previously uncertain input', queued=True)
            self.assertTrue(store.claim_owner_message(17, held))
            store.hold_uncertain_queued_claim([held])
            store.add_message(17, 'user', 'New saved work', queued=True)
            due = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
            store.transition_state({'sleeping': '1', 'sleep_until': due.isoformat()}, 'fixture sleep')
            self.assertTrue(controller.check_scheduled_wake(due + timedelta(seconds=1)))

            with patch('andrewbot.appserver.AppServerClient', return_value=client), \
                 patch('andrewbot.appserver.gmail_permissions', return_value={'enabled': False}), \
                 patch('andrewbot.appserver.time.monotonic', side_effect=lambda: clock[0]):
                reply = controller.flush_queued()

            self.assertEqual(reply, 'Finished saved work.')
            self.assertEqual(tools, ['shell.run'])
            self.assertEqual(store.get_state('autonomy_error', ''), '')
            # Successful saved-message delivery keeps Andrew available for a
            # follow-up, then allows autonomous work after the quiet window.
            self.assertEqual(store.get_state('session_start_requested'), 'conversation_quiet')
            self.assertEqual(store.uncertain_message_count(17), 1)
            self.assertEqual(store.pending_message_count(17), 1)
            inputs = [event for event in sent if event.get('type') == 'input']
            self.assertEqual(len(inputs), 1)
            text = inputs[0]['payload']['messages'][0]['content']
            self.assertIn('New saved work', text)
            self.assertNotIn('Previously uncertain input', text)
