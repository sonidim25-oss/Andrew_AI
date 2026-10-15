"""Offline Gmail reliability/permission checks; no real accounts or API calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from andrewbot.gmail_api import GmailAPI, GmailError
from andrewbot.gmail_policy import SCOPES
from andrewbot.gmail_tools import GmailTools, compose_message
from andrewbot.store import Store


class GmailReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.db')
        self.addCleanup(self.store.close)
        self.tools = GmailTools('/unused-fixture-vault', self.store)
        self.policy = {'schema': 1, 'enabled': True, 'read': True,
                       'draft': True, 'send': True, 'delete': True}
        self.api = Mock()
        self.api.scopes = set(SCOPES.values())
        self.api.request.return_value = {'id': 'fixture-message'}
        self.vault_factory = self.enterContext(patch('andrewbot.gmail_tools.Vault'))
        self.enterContext(patch('andrewbot.gmail_tools.GmailAPI', return_value=self.api))
        self.policy_reader = self.enterContext(patch('andrewbot.gmail_tools.load_permissions', side_effect=lambda: dict(self.policy)))
        self.args = {'request_id': 'fixture-request-01', 'to': ['fixture@example.com'],
                     'subject': 'Fixture', 'body': 'Local test only.'}

    def test_each_mutation_requires_its_own_permission_even_with_broad_scopes(self):
        for action in ('draft', 'send', 'delete'):
            with self.subTest(action=action):
                self.policy[action] = False
                with self.assertRaisesRegex(GmailError, 'disabled by the owner'):
                    self.tools.execute('gmail.' + action, self.args)
                self.policy[action] = True
        self.api.request.assert_not_called()
        self.vault_factory.assert_not_called()

    def test_owner_permission_without_oauth_scope_cannot_send(self):
        self.api.scopes = {SCOPES['read']}
        with self.assertRaisesRegex(GmailError, 'requires new OAuth consent'):
            self.tools.execute('gmail.send', self.args)
        self.api.request.assert_not_called()

    def test_confirmed_request_is_not_sent_twice_and_conflicting_reuse_fails(self):
        self.tools.execute('gmail.send', self.args)
        self.tools.execute('gmail.send', self.args)
        self.assertEqual(self.api.request.call_count, 1)
        with self.assertRaisesRegex(GmailError, 'different content'):
            self.tools.execute('gmail.send', {**self.args, 'body': 'Changed'})
        self.assertEqual(self.api.request.call_count, 1)

    def test_unknown_send_result_is_held_across_database_restart(self):
        self.api.request.side_effect = TimeoutError('private provider response')
        with self.assertRaisesRegex(GmailError, 'outcome uncertain'):
            self.tools.execute('gmail.send', self.args)
        second_store = Store(self.store.path)
        try:
            tools = GmailTools('/unused-fixture-vault', second_store)
            with self.assertRaisesRegex(GmailError, 'Prior Gmail outcome is uncertain'):
                tools.execute('gmail.send', self.args)
        finally:
            second_store.close()
        self.assertEqual(self.api.request.call_count, 1)

    def test_permission_revocation_during_refresh_blocks_mutation(self):
        self.api.access_token.side_effect = lambda: self.policy.update(send=False)
        with self.assertRaisesRegex(GmailError, 'disabled by the owner'):
            self.tools.execute('gmail.send', self.args)
        self.api.request.assert_not_called()

    def test_refresh_failure_does_not_claim_or_send_a_mutation(self):
        self.api.access_token.side_effect = GmailError('refresh failed')
        with self.assertRaises(GmailError):
            self.tools.execute('gmail.send', self.args)
        self.assertEqual(self.store.get_state('gmail_mutation:' + self.args['request_id'], ''), '')
        self.api.request.assert_not_called()

    def test_delete_moves_to_trash_only(self):
        self.tools.execute('gmail.delete', {'request_id': 'trash-fixture-01', 'message_id': 'fixture-message'})
        self.api.request.assert_called_once_with('messages/fixture-message/trash', 'POST', {})

    def test_read_does_not_issue_mutations(self):
        self.api.request.return_value = {'messages': [{'id': 'fixture-message'}]}
        self.tools.execute('gmail.read', {'operation': 'list'})
        self.assertEqual(self.api.request.call_count, 2)
        for call in self.api.request.call_args_list:
            self.assertEqual(len(call.args), 1)
            self.assertNotIn('payload', call.kwargs)

    def test_mail_header_injection_is_rejected(self):
        for change in ({'to': ['fixture@example.com\r\nBcc: other@example.com']},
                       {'subject': 'Hi\r\nBcc: other@example.com'}):
            with self.assertRaises(GmailError):
                compose_message({**self.args, **change})


class GmailRefreshTests(unittest.TestCase):
    def api(self):
        vault = Mock()
        vault.get_secret.return_value = json.dumps({'client_id': 'fixture-client',
            'client_secret': 'fixture-secret', 'refresh_token': 'fixture-refresh',
            'scope': SCOPES['read'], 'expires_at': 0})
        return GmailAPI(vault), vault

    def test_refresh_cannot_expand_scopes_or_restore_a_disconnected_grant(self):
        api, vault = self.api()
        response = {'access_token': 'fixture-access', 'token_type': 'Bearer',
                    'expires_in': 3600, 'scope': SCOPES['send']}
        with patch('andrewbot.gmail_api.token_request', return_value=response):
            with self.assertRaisesRegex(GmailError, 'scopes changed'):
                api.access_token()
        vault.set_secret.assert_not_called()

    def test_refresh_token_is_cached_only_in_the_current_operation(self):
        api, vault = self.api()
        with patch('andrewbot.gmail_api.token_request', return_value={
                'access_token': 'fixture-access', 'token_type': 'Bearer', 'expires_in': 3600}) as refresh:
            self.assertEqual(api.access_token(), 'fixture-access')
            self.assertEqual(api.access_token(), 'fixture-access')
            refresh.assert_called_once()
        vault.set_secret.assert_not_called()

    def test_expired_operation_cannot_begin_an_api_call(self):
        api, _ = self.api()
        api.deadline = 0
        with patch('andrewbot.gmail_api.google_request') as network:
            with self.assertRaisesRegex(GmailError, 'timed out'):
                api.request('messages')
        network.assert_not_called()
