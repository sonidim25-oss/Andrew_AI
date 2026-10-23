import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


class OwnerVoiceSetupTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('owner_voice_setup', Path(__file__).resolve().parents[1] / 'owner_voice_setup.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = self.root / 'owner-voice.conf'
        self.dropin = self.root / 'dropin.conf'
        self.owner = {'paused': '1', 'owner_control_revision': '7'}
        self.write = lambda path, data, mode: Path(path).write_bytes(data)

    def test_configures_voice_only_after_rechecking_pause_and_effective_process(self):
        with patch.object(self.module, 'paused_owner', return_value=self.owner), \
             patch.object(self.module, 'restart_controller') as restart, \
             patch.object(self.module, 'effective_voice', return_value=self.module.VOICE):
            self.module.configure_voice(self.owner, self.env, self.dropin, self.write)
        self.assertEqual(self.env.read_text(), 'ANDREW_TTS_VOICE=' + self.module.VOICE + '\n')
        self.assertIn('EnvironmentFile=' + str(self.env), self.dropin.read_text())
        restart.assert_called_once()

    def test_failed_effective_voice_restores_previous_configuration(self):
        self.env.write_text('ANDREW_TTS_VOICE=george\n')
        previous = self.env.read_bytes()
        with patch.object(self.module, 'paused_owner', return_value=self.owner), \
             patch.object(self.module, 'restart_controller') as restart, \
             patch.object(self.module, 'effective_voice', return_value='wrong'):
            with self.assertRaises(ValueError):
                self.module.configure_voice(self.owner, self.env, self.dropin, self.write)
        self.assertEqual(self.env.read_bytes(), previous)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(restart.call_count, 2)
