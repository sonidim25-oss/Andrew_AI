"""A runner rollout must retain the installed owner's routing and startup guards."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from andrewbot.controller import Controller
from andrewbot.media import MediaPolicy, MediaService
from andrewbot.store import Store


class RunnerDeployedCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'controller.sqlite3')
        self.addCleanup(self.store.close)

    def test_unicode_whitespace_pause_is_found_amid_media_backlog(self):
        def update(number, text):
            return dict(update_id=number, message={'from': {'id': 42},
                'chat': {'id': 42, 'type': 'private'}, 'text': text})
        for number in range(1, 55):
            self.store.enqueue_update(update(number, '/voice_reply\u2003hello'))
        self.store.enqueue_update(update(55, '\u2003/PAUSE@Andrew\u2003'))
        self.assertEqual([row[0] for row in self.store.pending_owner_controls(42)], [55])

    def test_disabled_voice_reply_does_not_start_model_or_queue_input(self):
        media = MediaService(self.store, self.root / 'media', 42,
                             MediaPolicy(enabled=True, voice_enabled=False))
        model = Mock()
        model.reply.return_value = ('unwanted model call', {})
        controller = Controller(self.store, model, 42, media=media)
        result = controller.handle_update(dict(update_id=1, message={'from': {'id': 42},
            'chat': {'id': 42, 'type': 'private'}, 'text': '/voice_reply hello'}))
        self.assertEqual(result, 'Voice generation is disabled.')
        model.reply.assert_not_called()
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_invalid_trusted_skill_stops_startup_before_service_credentials(self):
        from andrewbot import main
        vault = Mock()
        vault.names.return_value = ['telegram_bot', 'openrouter_api']
        vault.get_secret.return_value = 'fixture'
        catalog = Mock()
        catalog.validate.side_effect = ValueError('invalid trusted skill')
        with patch.dict(os.environ, {'ANDREW_OWNER_ID': '42'}), \
                patch.object(main, 'data_directory', return_value=self.root), \
                patch.object(main, 'Vault', return_value=vault), \
                patch.object(main, 'SkillCatalog', return_value=catalog, create=True), \
                patch.object(main, 'systemd_credential_bytes', side_effect=RuntimeError('unexpected credential access')) as credential:
            self.assertEqual(main.run(validate_only=True), 2)
            credential.assert_not_called()
