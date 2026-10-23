import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock


class OwnerVoicePreviewTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'owner_voice_preview.py'
        spec = importlib.util.spec_from_file_location('owner_voice_preview', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'preview'

    def test_exact_voice_sample_survives_restart_without_another_charge(self):
        provider = Mock()
        provider.generate.return_value = (b'ID3' + bytes(40), 'audio/mpeg', None)
        first = self.module.preview(self.root, provider)
        self.assertEqual(first['voice'], 'pvY1pikBdoI4SB62vEVo')
        self.assertEqual(provider.generate.call_args.args[1]['voice'], first['voice'])
        self.assertEqual(provider.generate.call_args.args[1]['model'], 'elevenlabs/eleven-v3')
        self.assertEqual(self.module.preview(self.root, provider), first)
        self.assertEqual(provider.generate.call_count, 1)

    def test_uncertain_provider_attempt_is_never_repeated(self):
        provider = Mock()
        provider.generate.side_effect = TimeoutError('PRIVATE provider detail')
        for _ in range(2):
            with self.assertRaises(ValueError) as caught:
                self.module.preview(self.root, provider)
            self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertEqual(provider.generate.call_count, 1)

    def test_tampered_saved_audio_is_rejected_without_regeneration(self):
        provider = Mock()
        provider.generate.return_value = (b'ID3' + bytes(40), 'audio/mpeg', None)
        saved = self.module.preview(self.root, provider)
        Path(saved['artifact']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.module.preview(self.root, provider)
        self.assertEqual(provider.generate.call_count, 1)
